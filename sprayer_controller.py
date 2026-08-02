import tkinter as tk
import customtkinter as ctk
from tkinter import ttk
from tkinter import messagebox as mb
import sys
import os
import math
import logging
import webbrowser
import requests
import subprocess
import numpy as np
import serial
import time
import json
import re
import threading
import queue
import websocket
from shapely.geometry import Polygon, LineString, Point
from shapely.affinity import rotate, translate

# =========================
# CONFIG
# =========================
CANVAS_W = 420
CANVAS_H = 420
SPRAYER_WIDTH = 2.5   # in millimeters; 2.5 is default for ucei sprayer
FEEDRATE = 1000  #default
OVERRUN = 0
NUM_PASSES = 1
BRUSH_ANGLE = 30  #in degrees
RECTANGLE = "Rectangle"
OVAL = "Oval"
SPIRAL = "Spiral"
ZIGZAG = "ZigZag"
CROSSHATCH = "Cross-Hatch"
OFFSET_RASTER = "Offset ZigZag"
ANGLED = "Angled Cross-Hatch"
ISOTROPIC = "Isotropic"
CIRCLE = "Circle"
CENTIMETERS = "cm"
MILLIMETERS = "mm"
INCHES = "in"
path_file = ""
CURRENT_X = 0.0
CURRENT_Y = 0.0
CURRENT_Z = 0.0
shape_to_draw = None
# =========================
# CONNECTION CONFIG + MODE  (single file; original defaults, optional override)
# =========================
# Built-in defaults match the original repo, so a bare copy on the Pi behaves
# exactly as before. An optional ucei_local.json next to this script (Windows /
# remote use) overrides these; a missing or malformed file is ignored.
OCTOPRINT_PORT = 5001
OCTOPRINT_URL = f"http://127.0.0.1:{OCTOPRINT_PORT}/"     # original default (Pi-local OctoPrint)
API_KEY = "ErDYaK23QBxF7Ka27f9zHV2sTz8MAHNWF76mROEJiuw"   # original repo key (Pi's OctoPrint)
ARDUINO_PORT = "/dev/ttyACM0"                            # original default


def _load_local_config():
    global OCTOPRINT_URL, API_KEY, ARDUINO_PORT
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ucei_local.json")
        with open(path) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return
        host = cfg.get("remote_host")
        if host:
            host = str(host)
            OCTOPRINT_URL = host if "://" in host else f"http://{host}:{OCTOPRINT_PORT}/"
            if not OCTOPRINT_URL.endswith("/"):
                OCTOPRINT_URL += "/"
        if cfg.get("api_key"):
            API_KEY = str(cfg["api_key"])
        if cfg.get("com_port"):
            ARDUINO_PORT = str(cfg["com_port"])
    except Exception:
        pass  # missing or malformed -> keep defaults, never crash


_load_local_config()


def _host_of(url):
    m = re.search(r"://([^:/]+)", url or "")
    return (m.group(1) if m else "127.0.0.1").lower()


def _compute_mode(platform_name, url):
    """Deployment mode from (platform, OctoPrint URL) -- testable in isolation.
    real_hardware: a printer physically on this box (Linux/Pi) OR a remote OctoPrint.
    startup_homing: the original startup G28 runs only for a loopback host."""
    loopback = _host_of(url) in ("127.0.0.1", "localhost", "::1")
    return {"loopback": loopback,
            "real_hardware": platform_name.startswith("linux") or not loopback,
            "startup_homing": loopback}


_MODE = _compute_mode(sys.platform, OCTOPRINT_URL)
LOOPBACK = _MODE["loopback"]
REAL_HARDWARE = _MODE["real_hardware"]
STARTUP_HOMING = _MODE["startup_homing"]
_FORCE_REAL_HARDWARE = False     # tests pin this
_ARMED = False                   # session ARM state (real hardware only; dev ignores it)


def _real_hardware():
    return REAL_HARDWARE or _FORCE_REAL_HARDWARE


def _motion_permitted():
    """True if motion/actuation bytes may leave the program on ANY channel now.
    Dev (loopback + non-Linux) is ungated; on real hardware, requires ARM MOTION."""
    return (not _real_hardware()) or _ARMED
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger()
# Keep third-party HTTP/socket debug chatter (urllib3, websocket) out of the
# GUI log box and console -- PrinterLink polls M114 frequently.
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)

class TextLogHandler(logging.Handler):
    """Logging handler that writes into a Tkinter Text widget.

    Tk is not thread-safe, so emit() (which may be called from background
    threads such as PrinterLink, including via urllib3's logging) only
    enqueues text; a periodic main-thread drain updates the widget.
    """
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        self._queue = queue.Queue()
        try:
            self.text_widget.after(200, self._drain)
        except Exception:
            pass

    def emit(self, record):
        try:
            self._queue.put_nowait(self.format(record))
        except Exception:
            pass

    def _drain(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                self.text_widget.config(state="normal")
                self.text_widget.insert("end", msg + "\n")
                self.text_widget.see("end")
                self.text_widget.config(state="disabled")
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.text_widget.after(200, self._drain)
        except Exception:
            pass

# =========================
# PATH GENERATORS
# =========================
def spiral_paths(poly, spacing):
    paths = []
    p = poly
    while p.area > spacing**2:
        paths.append(list(p.exterior.coords))
        p = p.buffer(-spacing)
        if p.is_empty:
            break
    return paths


def raster_paths_xdir(poly, spacing, overrun=None):  #used for crosshatch to fix overrun
    global OVERRUN
    if overrun is None:
        overrun = OVERRUN

    minx, miny, maxx, maxy = poly.bounds
    paths = []
    direction = 1
    for y in np.arange(miny, maxy, spacing):
        line = LineString([(minx, y), (maxx-spacing, y)])
        clipped = line.intersection(poly)

        if clipped.is_empty:
            continue

        segments = []
        if clipped.geom_type == "MultiLineString":
            segments = list(clipped)
        else:
            segments = [clipped]


        for seg in segments:
            coords = list(seg.coords)
            if len(coords) < 2:
                continue
            

            if direction < 0:
                coords = coords[::-1]


            # ---- EXTEND SEGMENT ----
            x1, y1 = coords[0]
            x2, y2 = coords[-1]

            dx = x2 - x1
            dy = y2 - y1
            length = np.hypot(dx, dy)

            if length == 0:
                continue

            ux = dx / length

            # extend both ends
            x1_ext = x1 - ux * overrun
            x2_ext = x2 + ux * overrun

            paths.append([(x1_ext, y1), (x2_ext, y2)])

        direction *= -1
    return paths


def raster_paths_ydir(poly, spacing, overrun=None):  #used for crosshatch - overrun is asymmetrical in default function because of rotation
    global OVERRUN
    if overrun is None:
        overrun = OVERRUN

    minx, miny, maxx, maxy = poly.bounds
    paths = []
    direction = 1
    for x in np.arange(minx, maxx, spacing):
        line = LineString([(x, miny), (x, maxy-spacing)])
        clipped = line.intersection(poly)

        if clipped.is_empty:
            continue

        segments = []
        if clipped.geom_type == "MultiLineString":
            segments = list(clipped)
        else:
            segments = [clipped]


        for seg in segments:
            coords = list(seg.coords)
            if len(coords) < 2:
                continue
            

            if direction < 0:
                coords = coords[::-1]


            # ---- EXTEND SEGMENT ----
            x1, y1 = coords[0]
            x2, y2 = coords[-1]

            dx = x2 - x1
            dy = y2 - y1
            length = np.hypot(dx, dy)

            if length == 0:
                continue

            uy = dy / length

            # extend both ends
            y1_ext = y1 - uy * overrun
            y2_ext = y2 + uy * overrun

            paths.append([(x1, y1_ext), (x2, y2_ext)])

        direction *= -1
    return paths


def raster_paths(poly, spacing, overrun=None):
    global OVERRUN
    if overrun is None:
        overrun = OVERRUN

    minx, miny, maxx, maxy = poly.bounds
    paths = []
    direction = 1
    for y in np.arange(miny, maxy, spacing):
        line = LineString([(minx, y), (maxx, y)])
        print("line string: ",line)
        clipped = line.intersection(poly)
        print("clipped: ",clipped)

        if clipped.is_empty:
            print("clip empty")
            continue

        segments = []
        if clipped.geom_type == "MultiLineString":
            segments = list(clipped)
        else:
            segments = [clipped]
        
        print("segments: ",segments)

        for seg in segments:
            coords = list(seg.coords)
            print("coords1: ", coords)
            
            if len(coords) < 2:
                continue
            
            if direction < 0:
                coords = coords[::-1]
            
            print("direction: ", direction)
            print("coords: ", coords)

            # ---- EXTEND SEGMENT ----
            x1, y1 = coords[0]
            x2, y2 = coords[-1]
            
            print(x1, y1)
            print(x2, y2)

            dx = x2 - x1
            dy = y2 - y1
            length = np.hypot(dx, dy)
            
            print("dx: ", dx)
            print("dy: ", dy)
            print("length: ", length)

            if length == 0:
                continue

            ux = dx / length
            uy = dy / length
            
            ## TESTING
            #x1_ext = x1 - OVERRUN      
            #y1_ext = y1 - OVERRUN
            #x2_ext = x2 + OVERRUN
            #y2_ext = y2 + OVERRUN

            # extend both ends
            x1_ext = x1 - (ux * overrun)      
            y1_ext = y1 - (uy * overrun) 
            x2_ext = x2 + (ux * overrun) 
            y2_ext = y2 + (uy * overrun) 
            
            print("x1, y1_ext: ", x1_ext, y1_ext)
            print("x2, y2_ext: ", x2_ext, y2_ext)
            

            paths.append([(x1_ext, y1_ext), (x2_ext, y2_ext)])

        direction *= -1
        
        
    # 1. Create a line exactly at the top boundary
    top_line = LineString([(minx, maxy), (maxx, maxy)])
    clipped_top = top_line.intersection(poly)

    if not clipped_top.is_empty:
        top_segments = [clipped_top] if clipped_top.geom_type != "MultiLineString" else list(clipped_top)
       
        for seg in top_segments:
            coords = list(seg.coords)
            if len(coords) >= 2:
                x1, y1 = coords[0]
                x2, y2 = coords[-1]
               
                # Apply the same exact vector and overrun math
                dx = x2 - x1
                dy = y2 - y1
                length = np.hypot(dx, dy)
               
                if length > 0:
                    ux = dx / length
                    uy = dy / length
                   
                    x1_ext = x1 - ux * overrun
                    y1_ext = y1 - uy * overrun
                    x2_ext = x2 + ux * overrun
                    y2_ext = y2 + uy * overrun
                   
                    top_path = [(x1_ext, y1_ext), (x2_ext, y2_ext)]
                   
                    # Respect the zigzag direction
                    if direction < 0:
                        top_path = top_path[::-1]
                       
                    paths.append(top_path)
                   
        direction *= -1
        
    return paths

def offset_raster_path(poly, spacing, numofpasses=1):
    all_paths = []
    print(numofpasses)
    for i in np.arange(0, spacing, spacing/numofpasses):
        print("I: ", i)
        rot = translate(poly, xoff=0, yoff=i)
        
        raster = raster_paths_xdir(rot, spacing)

        for path in raster:
            restored = []
            for x, y in path:
                p_final = Point(x, y)

                restored.append(p_final.coords[0])
            if len(restored) >= 2:
                all_paths.append(restored)
        if i < numofpasses - 1:
                original_paths.append("DWELL")
    return all_paths


def isotropic_paths(poly, spacing):
    """
    Combines Standard (0, 90) and Angled (45, 135) for 
    maximum coating uniformity.
    """
    all_paths = []
    # This combines both sets of angles into one list of G-code paths
    for angle in [0, 90, 45, 135]:
        # safe_poly = poly.buffer(spacing)
        rot = rotate(poly, angle, origin='centroid')   #rotates the spray angle

        #offsets the angled pattern to help reduce intersections
        # if angle in [45, 135]:
        #     rot = translate(rot, xoff=spacing/2, yoff=spacing/2)  

        raster = raster_paths(rot, spacing)

        for path in raster:
            restored = []
            for x, y in path:
                # if angle in [45, 135]:
                #     p = translate(Point(x, y), xoff=-spacing/2, yoff=-spacing/2) #shifts offset back to 0
                # else:
                #     p = Point(x, y)

                p_final = rotate(Point(x, y), -angle, origin=poly.centroid)
                restored.append(p_final.coords[0])
            if len(restored) >= 2:
                all_paths.append(restored)
        
        #after spraying at 0 and 90 degrees, we want to wait 3o seconds before spraying angled patterns
        if angle == 90:
            all_paths.append("DWELL")  
    return all_paths

def crosshatch_paths(poly, spacing):
    all_paths = []
    
    # ~ fixed_origin = poly.centroid
    
    # ~ for angle in [0, 90]:
        # ~ rot = rotate(poly, angle, origin=fixed_origin)
        # ~ raster = raster_paths(rot, spacing)

        # ~ for path in raster:
            # ~ restored = []
            # ~ for x, y in path:
                # ~ p = rotate(Point(x, y), -angle, origin=fixed_origin)
                # ~ restored.append(p.coords[0])
            # ~ if len(restored) >= 2:
                # ~ all_paths.append(restored)

    horizontal_lines = raster_paths_xdir(poly, spacing, overrun=10)
    vertical_lines = raster_paths_ydir(poly, spacing, overrun=10)

    all_paths=horizontal_lines+vertical_lines

    return all_paths

def angled_crosshatch_paths(poly, spacing):
    all_paths = []
    for angle in [45, 135]:
        rot = rotate(poly, angle, origin='centroid')
        raster = raster_paths(rot, spacing)

        for path in raster:
            restored = []
            for x, y in path:
                p = rotate(Point(x, y), -angle, origin=poly.centroid)
                restored.append(p.coords[0])
            if len(restored) >= 2:
                all_paths.append(restored)

    return all_paths


def metric_to_mm_converter(value, metric): # converts metric to mm
    numeric_value = float(value)
    if metric == CENTIMETERS:
        scale_factor = 10
    elif metric == INCHES:
        scale_factor = 25.4
    elif metric == MILLIMETERS:
        scale_factor = 1.0
    else:
        scale_factor = 1.0
    mm = numeric_value * scale_factor
    return mm

# =========================
# G-CODE WRITER
# =========================
def write_gcode(filename, paths):
    x_start, y_start, z_start = [80, 98, 0]  #work home coordinates
    #checks for specified servo(needle) height
    try:
        user_input_angle = servoDegreetb.get()
        if not user_input_angle: # If the textbox is empty
            servo_angle = 0
        else:
            if int(user_input_angle) >= 0 and int(user_input_angle) <= 270:
                servo_angle = int(user_input_angle)
            else:
                servo_angle = 0
                logger.warning("Invalid servo angle, defaulting to 0.")
    except (ValueError, NameError):
        # If the input isn't a number or the textbox isn't found
        servo_angle = 0
        logger.warning("Invalid or missing servo angle, defaulting to 0.")

    # Continuous Spray toggle (Additional-tab checkbox). When ON, the sprayer
    # servo is turned on once at the start and off once after the final line,
    # holding the angle through every travel move (no per-line toggles/dwells).
    try:
        continuous_spray = bool(continuous_spray_var.get())
    except Exception:
        continuous_spray = False
    

    #checks for specified z height
    # try:
    #     zheight = heightEntry.get()
    #     if not zheight: # If the textbox is empty
    #         pass
    #     else:
    #         if int(zheight) >= 0 and int(zheight) <= 35:
    #             z_start = int(zheight)
    #         else:
    #             z_start = 0
    #             logger.warning("Invalid height, defaulting to 0.")
    # except (ValueError, NameError):
    #     # If the input isn't a number or the textbox isn't found
    #     z_start = 0
    #     logger.warning("Invalid or missing height, defaulting to 0.")

    z_start = 0
    # ~ xnew = x_start - (z_start*math.tan(math.radians(BRUSH_ANGLE)))  #dynamic homing with zheight
    xnew = x_start    #comment out if you using line above ^
    

    with open(filename, "w") as f:
        f.write("G21\n")      # mm
        f.write("G90\n")      # absolute
        f.write("M211 S0\n")      # disables software endstops
        f.write("G28 X Y\n")  #Moves to machine home (where limit switches click)
        f.write("G92 Z0\n")  #remove once we get limit switches
        f.write("G0 Z1\n")
        f.write("G0 Z-1\n")
        # f.write(f"G92 X Y\n")     # moves to work home. work home set by user
        f.write("G54\n")
        f.write("G0 X0 Y0\n")

        # Continuous Spray: turn the sprayer ON once here and hold it through
        # all travels; the per-line toggles below are skipped when continuous.
        if continuous_spray:
            f.write(f"M280 P0 S{servo_angle}\n")   # Spray ON (once, at path start)
            f.write("G4 P250\n")                    #Dwell 250ms for servo to move

        E=0 #needed for extrusion. octoprint is for 3d printers so if extrusion isn't mentioned, it thinks that nothing is happening

        for path in paths:    
            print("path: ",path)
            if path == "DWELL":
                if not continuous_spray:
                    f.write("M280 P0 S0\n") # Spray OFF (servo stays on across passes when continuous)
                f.write("G0 X0 Y0\n") # 1. Park the nozzle at origin to avoid heat
                f.write("G4 S5\n")    # 2. Dwell for 5 seconds
                continue               # 3. Move to the next pass (45 degrees)    
            x0, y0 = path[0]
            # x_end, y_end = path[-1]

            f.write(f"G0 X{x0:.2f} Y{y0:.2f}\n")

            # Spray OFF (skipped when continuous -- servo held on through travel)
            if not continuous_spray:
                f.write("M280 P0 S0\n")
                f.write("G4 P250\n")  #Dwell 250ms for servo to move

            # # used for servo jitter to prevent clogging
            # step_size = 5.0 #5 mm
            # servo_clogging_angle = 3
            # dist = math.hypot(x_end - x0, y_end - y0)
            # num_segments = max(1, int(dist / step_size))

            # for i in range(1, num_segments+1):
            #     E+=1
            #     x = x0 + (x_end-x0) * (i/num_segments)
            #     y = y0 + (y_end-y0) * (i/num_segments)

            #     f.write(f"M280 P0 S{servo_angle + servo_clogging_angle}\n")
            #     f.write(f"G1 X{x:.2f} Y{y:.2f} E{E} F{FEEDRATE}\n")

            #     servo_clogging_angle *= -1
            
            for x, y in path:
                E+=1
                f.write(f"G1 X{x:.2f} Y{y:.2f} E{E} F{FEEDRATE}\n")

            # Spray ON (skipped when continuous -- servo already on from path start)
            if not continuous_spray:
                f.write(f"M280 P0 S{servo_angle}\n")
                f.write("G4 P250\n")  #Dwell 250ms for servo to move
            
        f.write("M280 P0 S0\n")
        f.write("G4 P250\n")  #Dwell 250ms for servo to move

        # ===== Shutdown =====
        # Return to origin
        f.write("G28 X Y\n")
        f.write(f"G1 Z-{z_start}\n")
        #f.write(f"G0 X-{xnew:.1f} Y-{y_start} Z-{z_start}\n")

        logger.info(f"Gcode file is generated with servo angle:{servo_angle}")



# def heat_bed():  #updated marlin function
#     global ser
#     global new_temp
#     temp =  # get the desired temp
#     if temp == "" or not isinstance(int(temp), int):  # no input / incorrect value
#         logger.warning("Please input a valid integer")
#         new_temp = 0
#         return
#     elif int(temp) >= 0 and int(temp) <= 100: #valid temp set
#         new_temp = int(temp)
#         logger.info(f"New temperature = {new_temp}")
        
#         headers = {
#             "X-Api-Key": API_KEY,
#             "Content-Type": "application/json"
#         }

#         payload = {"command": f"M140 S{new_temp}"}

#         try:
#             response = requests.post(f"{OCTOPRINT_URL}api/printer/command", headers=headers, json=payload)
#             if response.status_code == 204:
#                 logger.info("Successfully sent servo the temperature command to OctoPrint!")
#             else:
#                 logger.error(f"Error: {response.status_code} - {response.text}")
#         except Exception as e:
#             logger.error(f"Failed to connect to OctoPrint / move servo: {e}")

#     else:  # if temp specified is too high or low / invalid input, pass 
#         new_temp = 0
#         logger.warning("Inputted integer outside of bounds. Select a temperature between 0 & 270")
#         return
#     return

def cm_to_mm_converter(coords): # converts cm to mm
    CM_TO_MM = 10
    square_mm = [(x*CM_TO_MM, y*CM_TO_MM) for x,y in coords]
    return square_mm

# CIRCULAR SUBSTRATE - default test
def default_circle_path_generator(): 
    circle = Point(0, 0).buffer(5*10)  #buffer = radius; 10 is the converter (to cm->mm)

    spiral = spiral_paths(circle, SPRAYER_WIDTH)
    raster = raster_paths(circle, SPRAYER_WIDTH)
    cross  = crosshatch_paths(circle, SPRAYER_WIDTH)

    write_gcode("spiral.nc", spiral)
    write_gcode("raster.nc", raster)
    write_gcode("crosshatch.nc", cross)

# 5 INCH SQUARE SUBSTRATE - default test
def default_rect_path_generator(): 
    pointss = [(0,0), (5,0), (5,5), (0,5)] #in centimeters
    square_mm = cm_to_mm_converter(pointss)
    poly = Polygon(square_mm)

    spiral = spiral_paths(poly, SPRAYER_WIDTH)
    raster = raster_paths(poly, SPRAYER_WIDTH)
    cross  = crosshatch_paths(poly, SPRAYER_WIDTH)

    write_gcode("spiral.nc", spiral)
    write_gcode("raster.nc", raster)
    write_gcode("crosshatch.nc", cross)



##########################
# HELPER FUNCTIONS
##########################
def _motion_allowed(action_desc=""):
    """Gate for interactive motion/actuation. No per-action dialog any more:
    dev (loopback + non-Linux) is ungated; on real hardware motion is permitted
    only while ARM MOTION is on. Arming is the consent. See _motion_permitted()."""
    return _motion_permitted()


def move_servo():  #updated marlin function
    global ser
    angle = servoDegreetb.get()
    if angle == "" or not isinstance(int(angle), int):  # no input / incorrect value
        logger.warning("Please input a valid integer")
        new_angle = 0
        return
    elif int(angle) >= 0 and int(angle) <= 270: #valid angle set
        new_angle = int(angle)
        logger.info(f"New angle = {new_angle}")
        
        headers = {
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json"
        }
        
        if not _motion_allowed(f"Move servo to {new_angle} deg  (M280 P0 S{new_angle})"):
            return
        commands = [f"M280 P0 S{new_angle}","G4 S3", "M280 P0 S0"]
        _plink.note_servo(new_angle)   # update the commanded-angle indicator immediately

        payload = {"commands": commands}
        
        # payload = {"command": f"M280 P0 S{new_angle}"}

        try:
            response = requests.post(f"{OCTOPRINT_URL}api/printer/command", headers=headers, json=payload)
            if response.status_code == 204:
                logger.info("Successfully sent servo command to OctoPrint!")
            else:
                logger.error(f"Error: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Failed to connect to OctoPrint / move servo: {e}")

    else:  # if angle specified is too high or low / invalid input, pass 
        new_angle = 0
        logger.warning("Inputted integer outside of bounds. Select an angle between 0 & 270")
        return
    return

# =========================
# PRINTER LINK (OctoPrint push socket) + JOG
# =========================
_POS_RE = re.compile(r"X:(-?\d+(?:\.\d+)?)\s+Y:(-?\d+(?:\.\d+)?)\s+Z:(-?\d+(?:\.\d+)?)")
_MARKER_M280_RE = re.compile(r"M280 P0 S(\d+)")


class PrinterLink:
    """Background OctoPrint push-socket client.

    Tracks live printer state flags and the reported X/Y/Z position (parsed
    from M114 replies in the terminal stream). Injects M114 on a throttle --
    every ~2s while idle, at most every 5s during a job so it never stalls a
    running print. GUI reads go through snapshot()/can_jog(); it never touches
    Tk widgets (Tk is updated only from the main thread in update_gui_coordinates).
    """
    def __init__(self, base_url, api_key):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._lock = threading.Lock()
        self._flags = {}
        self._pos = None          # (x, y, z) or None when unknown
        self._pos_time = 0.0      # when _pos last updated (from an M114 reply)
        self._servo_angle = None  # last commanded servo angle (M280 S<n>); None until first seen
        self._progress = None     # job completion fraction (0..1) or None
        self._job_file = None     # currently-selected job file name (for the Print button)
        self._log_q = queue.Queue(maxsize=4000)  # comm-log lines mirrored to the terminal panel
        self._socket_up = False
        self._last_frame = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="PrinterLink", daemon=True)
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return {"socket": self._socket_up, "flags": dict(self._flags),
                    "pos": self._pos, "age": time.time() - self._last_frame,
                    "pos_age": time.time() - self._pos_time,
                    "servo_angle": self._servo_angle, "progress": self._progress,
                    "job_file": self._job_file}

    # State predicates evaluated from a SINGLE snapshot (no torn reads). A stale
    # socket (no push frame within STALE_S seconds) is treated as not-operational
    # so the guard never trusts a half-open connection.
    STALE_S = 8.0

    @classmethod
    def _operational(cls, s):
        return bool(s["socket"] and s["flags"].get("operational")
                    and not s["flags"].get("closedOrError") and s["age"] < cls.STALE_S)

    @staticmethod
    def _busy(s):
        f = s["flags"]
        return bool(f.get("printing") or f.get("paused") or f.get("pausing")
                    or f.get("cancelling") or f.get("resuming") or f.get("finishing"))

    def is_operational(self):
        return self._operational(self.snapshot())

    def is_busy(self):
        return self._busy(self.snapshot())

    def can_jog(self):
        # jog only when a FRESH socket says operational and not mid-job (single snapshot)
        s = self.snapshot()
        return bool(self._operational(s) and not self._busy(s))

    def _headers(self):
        return {"X-Api-Key": self._key, "Content-Type": "application/json"}

    def _send_m114(self):
        try:
            requests.post(self._base + "/api/printer/command",
                          headers=self._headers(), json={"command": "M114"}, timeout=4)
        except Exception:
            pass

    def note_servo(self, angle):
        """Record a servo angle the GUI itself just commanded (zero-lag update;
        the comm-log stream will also carry it -- same field, one source of truth)."""
        try:
            with self._lock:
                self._servo_angle = int(angle)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:
                logger.debug(f"PrinterLink session ended: {e}")
            with self._lock:
                self._socket_up = False
                self._pos = None
                self._flags = {}
            self._stop.wait(3.0)   # backoff before reconnect

    def _session(self):
        r = requests.post(self._base + "/api/login", headers=self._headers(),
                          json={"passive": True}, timeout=6)
        j = r.json()
        name, session = j.get("name"), j.get("session")
        if not (name and session):
            raise RuntimeError("no push-socket session from /api/login")
        ws = websocket.create_connection(
            self._base.replace("http", "ws") + "/sockjs/websocket", timeout=1.5)
        try:
            ws.send(json.dumps({"auth": f"{name}:{session}"}))
            with self._lock:
                self._socket_up = True
            last_m114 = 0.0
            while not self._stop.is_set():
                now = time.time()
                if self.is_operational():
                    interval = 5.0 if self.is_busy() else 2.0
                    if now - last_m114 >= interval:
                        self._send_m114()
                        last_m114 = now
                try:
                    frame = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not frame:
                    continue
                try:
                    msg = json.loads(frame)
                except Exception:
                    continue
                cur = msg.get("current")
                if not cur:
                    continue
                fl = cur.get("state", {}).get("flags")
                prog = (cur.get("progress") or {}).get("completion")
                jobf = ((cur.get("job") or {}).get("file") or {}).get("name")
                with self._lock:
                    self._last_frame = time.time()
                    if fl:
                        self._flags = fl
                    if prog is not None:
                        self._progress = prog / 100.0
                    self._job_file = jobf
                    for ln in cur.get("logs", []) or []:
                        try:
                            self._log_q.put_nowait(ln)   # mirror the comm stream to the terminal
                        except queue.Full:
                            pass
                        sm = _MARKER_M280_RE.search(ln)
                        if sm:
                            self._servo_angle = int(sm.group(1))   # commanded angle (job/terminal/external)
                        m = _POS_RE.search(ln)
                        if m:
                            self._pos = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                            self._pos_time = time.time()
        finally:
            try:
                ws.close()
            except Exception:
                pass


_plink = PrinterLink(OCTOPRINT_URL, API_KEY)


def update_gui_coordinates():
    # Socket-driven position readout + jog-control gating (replaces the old
    # dead-reckoning). Runs on the Tk main thread via root.after.
    s = _plink.snapshot()
    operational = PrinterLink._operational(s)
    if s["pos"] is not None and operational:
        x, y, z = s["pos"]
        x_coord_str.set(f"X: {x:.1f} mm")
        y_coord_str.set(f"Y: {y:.1f} mm")
        z_coord_str.set(f"Z: {z:.1f} mm")
    else:
        x_coord_str.set("X: —")
        y_coord_str.set("Y: —")
        z_coord_str.set("Z: —")
    jog_ok = operational and not PrinterLink._busy(s) and _motion_permitted()  # real hw needs ARM
    new_state = "normal" if jog_ok else "disabled"
    for w in _jog_widgets:
        try:
            w.configure(state=new_state)
        except Exception:
            pass
    # servo commanded-angle indicator (there is no feedback from the servo)
    try:
        ang = s.get("servo_angle")
        servo_angle_str.set("Servo (commanded): %d°" % ang if ang is not None
                            else "Servo (commanded): —")
    except Exception:
        pass
    # terminal input/send are enabled only while connected
    try:
        term_state = "normal" if operational else "disabled"
        terminal_input.configure(state=term_state)
        terminal_send_button.configure(state=term_state)
    except Exception:
        pass
    # machine controls: ARM toggle, one-click Print (filename/reason), Pause/Cancel
    try:
        arm_switch.configure(state="normal" if (operational and _real_hardware()) else "disabled")
        jf = s.get("job_file")
        if not operational:
            print_button.configure(state="disabled", text="Print — connect printer")
        elif not jf:
            print_button.configure(state="disabled", text="Print — select a file")
        elif _real_hardware() and not _ARMED:
            print_button.configure(state="disabled", text="Print — ARM first")
        else:
            print_button.configure(state="normal",
                                   text=("Resume: " if s["flags"].get("paused") else "Print: ") + str(jf))
        pc = "normal" if operational else "disabled"     # Pause/Cancel always live while connected
        pause_button.configure(state=pc)
        cancel_button.configure(state=pc)
    except Exception:
        pass
    try:
        root.after(500, update_gui_coordinates)
    except Exception:
        pass


def _get_jog_feedrate():
    try:
        v = float(jog_feedrate_entry.get())
        if math.isfinite(v) and v > 0:
            return v
    except Exception:
        pass
    return 1500.0  # default mm/min


def _jog_step():
    # step selector values look like "0.1 mm" / "1 mm" / "10 mm"
    try:
        return float(jog_step_option.get().split()[0])
    except Exception:
        return 1.0


def _do_jog(dx=0.0, dy=0.0, dz=0.0):
    if not _plink.can_jog():
        logger.info("Jog ignored: printer not connected/operational or busy.")
        return
    step = _jog_step()
    fr = _get_jog_feedrate()
    move = {}
    if dx:
        move["x"] = dx * step
    if dy:
        move["y"] = dy * step
    if dz:
        move["z"] = dz * step
    if not move:
        return
    if not _motion_allowed(f"Jog {move} at {fr:.0f} mm/min"):
        return
    if not _plink.can_jog():  # re-check: state may have changed during confirmation
        logger.info("Jog aborted: printer no longer connected/idle after confirmation.")
        return
    try:
        r = requests.post(f"{OCTOPRINT_URL}api/printer/printhead",
                          headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
                          json={"command": "jog", "speed": fr, "absolute": False, **move}, timeout=6)
        if r.status_code not in (200, 204):
            logger.error(f"Jog failed: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Jog send failed: {e}")


def _do_home(axes):
    if not _plink.can_jog():
        logger.info("Home ignored: printer not connected/operational or busy.")
        return
    if not _motion_allowed(f"Home {'/'.join(axes).upper()}"):
        return
    if not _plink.can_jog():  # re-check: state may have changed during confirmation
        logger.info("Home aborted: printer no longer connected/idle after confirmation.")
        return
    try:
        r = requests.post(f"{OCTOPRINT_URL}api/printer/printhead",
                          headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
                          json={"command": "home", "axes": axes}, timeout=6)
        if r.status_code not in (200, 204):
            logger.error(f"Home failed: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Home send failed: {e}")


def _post_job(body):
    if not _plink.is_operational():
        return None
    try:
        return requests.post(f"{OCTOPRINT_URL}api/job",
                             headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
                             json=body, timeout=6)
    except Exception as e:
        logger.error(f"Job command failed: {e}")
        return None


def _do_print():
    # One click: start the selected file (or resume if paused). On real hardware ARM
    # is the consent -- no confirmation dialog. Dev is ungated. Motion-gated.
    s = _plink.snapshot()
    if not (PrinterLink._operational(s) and _motion_permitted()):
        return
    if s["flags"].get("paused"):
        _post_job({"command": "pause", "action": "resume"})
    elif s.get("job_file"):
        _post_job({"command": "start"})


def _do_pause():
    _post_job({"command": "pause", "action": "pause"})   # always live while connected


def _do_cancel():
    _post_job({"command": "cancel"})                     # always live while connected


def _set_armed(val):
    global _ARMED
    _ARMED = bool(val)
    logger.warning("ARM MOTION %s" % ("ON — interactive motion enabled" if _ARMED
                                      else "off — motion locked out"))


# =========================
# LIVE AIRBRUSH MARKER (canvas)
# =========================
def machine_to_canvas(mx, my):
    """Map machine (mm) X/Y to canvas pixels using the SAME transform the shape
    drawing established: the shape's machine bbox (shape_original_coords) maps to
    its drawn canvas bbox (canvas.coords(shape_to_draw))."""
    if shape_to_draw is None:
        return None
    try:
        mx0, my0, mx1, my1 = shape_original_coords
        cx0, cy0, cx1, cy1 = canvas.coords(shape_to_draw)
    except Exception:
        return None
    if mx1 == mx0 or my1 == my0:
        return None
    return (cx0 + (mx - mx0) * (cx1 - cx0) / (mx1 - mx0),
            cy0 + (my - my0) * (cy1 - cy0) / (my1 - my0))


def _flatten_path(paths):
    pts = []
    for seg in paths or []:
        if seg == "DWELL":
            continue
        for p in seg:
            pts.append((float(p[0]), float(p[1])))
    return pts


def _interp_along(pts, frac):
    """Point at `frac` (0..1) of the arc length along the flattened toolpath."""
    if not pts:
        return None
    if len(pts) == 1:
        return pts[0]
    d = [0.0]
    for i in range(1, len(pts)):
        d.append(d[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    total = d[-1]
    if total <= 0:
        return pts[0]
    target = max(0.0, min(1.0, frac)) * total
    for i in range(1, len(pts)):
        if d[i] >= target:
            seg = d[i] - d[i - 1]
            t = 0.0 if seg == 0 else (target - d[i - 1]) / seg
            return (pts[i - 1][0] + t * (pts[i][0] - pts[i - 1][0]),
                    pts[i - 1][1] + t * (pts[i][1] - pts[i - 1][1]))
    return pts[-1]


def compute_marker():
    """State for the airbrush marker: {machine, canvas, spraying, estimated}.
    Idle -> reported M114 position (solid). During a job -> interpolate along the
    generated path by file-progress (estimated/hollow) and snap to a real M114
    position whenever a fresh one arrives (solid)."""
    s = _plink.snapshot()
    spraying = bool(s.get("servo_angle"))   # one source of truth: the commanded angle
    printing = bool(s["flags"].get("printing"))
    pos = s.get("pos")
    pos_fresh = s.get("pos_age", 1e9) < 1.5   # a real position line just arrived -> snap
    if printing and not pos_fresh and s.get("progress") is not None:
        mp = _interp_along(_flatten_path(globals().get("original_paths") or []), s["progress"])
        if mp is not None:
            return {"machine": mp, "canvas": machine_to_canvas(*mp),
                    "spraying": spraying, "estimated": True}
    if pos is not None:
        return {"machine": (pos[0], pos[1]), "canvas": machine_to_canvas(pos[0], pos[1]),
                "spraying": spraying, "estimated": False}
    return {"machine": None, "canvas": None, "spraying": spraying, "estimated": False}


def draw_marker(m):
    canvas.delete("airbrush_marker")
    if not m or m.get("canvas") is None:
        return
    cx, cy = m["canvas"]
    color = "#ff3d3d" if m["spraying"] else "#4a6572"   # red = spraying, slate = off
    r = 7
    canvas.create_line(cx - r - 5, cy, cx + r + 5, cy, fill=color, width=2, tags="airbrush_marker")
    canvas.create_line(cx, cy - r - 5, cx, cy + r + 5, fill=color, width=2, tags="airbrush_marker")
    if m["estimated"]:   # hollow ring = estimated (job interpolation)
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=2, tags="airbrush_marker")
    else:                # solid dot = reported (M114)
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color, tags="airbrush_marker")


def update_marker():
    try:
        draw_marker(compute_marker())
    except Exception:
        pass
    try:
        root.after(200, update_marker)
    except Exception:
        pass


# =========================
# TERMINAL PANEL (OctoPrint comm)
# =========================
# Gcode words that count as motion/actuation and therefore need the PI motion
# confirmation. Kept in ONE place so terminal + any future caller share the list.
# Motion/actuation words (for classification/tests). The terminal GATE itself is an
# allowlist: only QUERY_COMMANDS (+ e-stop) send while disarmed on real hardware.
MOTION_COMMANDS = frozenset({"G0", "G1", "G2", "G3", "G5", "G6", "G28", "G38",
                             "G92", "M280", "M290", "M42"})
QUERY_COMMANDS = frozenset({"M105", "M114", "M115"})   # always safe to send
ALWAYS_ALLOWED = frozenset({"M112"})                   # emergency stop -- never gated

_terminal_history = []
_terminal_hist_idx = [0]


def _terminal_command_allowed(cmd):
    """M112 e-stop always; dev or ARMed real hardware -> everything; disarmed real
    hardware -> queries only (fail-safe: unknown/unlisted words are blocked)."""
    w = _command_word(cmd)
    if w in ALWAYS_ALLOWED:
        return True
    if not _real_hardware() or _ARMED:
        return True
    return w in QUERY_COMMANDS


def _command_word(cmd):
    """First real gcode word the way the firmware sees it: drop a leading line
    number (N123) and any '(...)'/';' comments, then read the G/M word.
    'N10 M280 P0 S5'->'M280'; 'G1 (move) X10'->'G1'; 'g00 X1'->'G0'."""
    if cmd is None:
        return None
    s = re.sub(r"\(.*?\)", " ", cmd)       # drop (...) comments
    s = s.split(";", 1)[0]                  # drop ; comment
    s = re.sub(r"^\s*N\d+\b", "", s)         # drop a leading line number
    m = re.match(r"\s*([GgMm])\s*(\d+)", s)
    return ("%s%d" % (m.group(1).upper(), int(m.group(2)))) if m else None


def _is_motion_command(cmd):
    return _command_word(cmd) in MOTION_COMMANDS


def _terminal_send():
    if not _plink.is_operational():
        logger.info("Terminal: printer not connected; nothing sent.")
        return
    raw = terminal_input.get("1.0", "end")
    cmds = []
    for line in raw.splitlines():
        line = line.split(";", 1)[0].strip()   # strip inline/line comments and blanks
        if line:
            cmds.append(line)
    terminal_input.delete("1.0", "end")
    if not cmds:
        return
    _terminal_history.append("\n".join(cmds))
    _terminal_hist_idx[0] = len(_terminal_history)
    to_send = []
    for c in cmds:
        if not _terminal_command_allowed(c):
            logger.info("Terminal: blocked (%s): %s"
                        % ("disarmed" if _real_hardware() else "not permitted", c))
            continue
        to_send.append(c)
        if _command_word(c) == "M280":            # keep the angle indicator in lock-step
            sm = re.search(r"[Ss]\s*(\d+)", c)
            if sm:
                _plink.note_servo(int(sm.group(1)))
    if not to_send:
        return
    try:
        r = requests.post(f"{OCTOPRINT_URL}api/printer/command",
                          headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
                          json={"commands": to_send}, timeout=6)
        if r.status_code not in (200, 204):
            logger.error(f"Terminal send failed: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Terminal send failed: {e}")


def _terminal_history_nav(direction):
    if not _terminal_history:
        return "break"
    idx = max(0, min(len(_terminal_history), _terminal_hist_idx[0] + direction))
    _terminal_hist_idx[0] = idx
    terminal_input.delete("1.0", "end")
    if idx < len(_terminal_history):
        terminal_input.insert("1.0", _terminal_history[idx])
    return "break"


def _terminal_drain():
    # Mirror OctoPrint's comm stream (from the SAME push socket) into the log.
    try:
        appended = False
        while True:
            try:
                line = _plink._log_q.get_nowait()
            except queue.Empty:
                break
            terminal_log.configure(state="normal")
            terminal_log.insert("end", line + "\n")
            appended = True
        if appended:
            count = int(terminal_log.index("end-1c").split(".")[0])
            if count > 800:
                terminal_log.delete("1.0", f"{count-600}.0")
            if terminal_autoscroll_var.get():
                terminal_log.see("end")
            terminal_log.configure(state="disabled")
    except Exception:
        pass
    try:
        root.after(150, _terminal_drain)
    except Exception:
        pass


def is_integer(s):
    try:
        int(s)
        return True
    except ValueError:
        return False
        
def is_float(s):
    try:
        float(s)
        return True
    except ValueError:
        return False

def setNumPasses():
    global NUM_PASSES
    value = numPassestb.get()
    if value == "":
        return # if nothing entered use current NUM_PASSES
    try:
        n = int(value)
        if n > 0:
            NUM_PASSES = n
            logger.info(f"Number of passes set to: {NUM_PASSES}")
        else:
            mb.showwarning("Number of passes must be a positive integer")
    except ValueError:
        mb.showwarning("Please enter a whole number for passes")

def setSprayerWidth():
    global SPRAYER_WIDTH
    value = sprayerWidthtb.get()
    if value == "":
        return # if nothing entered use current SPRAYER_WIDTH
    try:
        w = float(value)
        if w > 0:
            SPRAYER_WIDTH = w
            logger.info(f"Sprayer width set to: {SPRAYER_WIDTH}mm")
        else:
            mb.showwarning("Sprayer width must be a positive number")
    except ValueError:
        mb.showwarning("Please enter a valid number for sprayer width")

def setOverrun():
    global OVERRUN
    value = overruntb.get()
    if value == "":
        return   # if nothing entered use current OVERRUN
    try:
        o = float(value)
        if o >= 0:
            OVERRUN = o
            logger.info(f"Overrun set to: {OVERRUN}mm")
        else:
            mb.showwarning("Overrun cannot be negative")
    except ValueError:
        mb.showwarning("Please enter a valid number for overrun")

def setFeedrate():
    global FEEDRATE
    value = feedratetb.get()
    if value == "":
        return   # if nothing entered use current FEEDRATE
    try:
        f = int(value)
        if f > 0:
            FEEDRATE = f
            logger.info(f"Feedrate set to: {FEEDRATE} mm/min")
        else:
            mb.showwarning("Feedrate must be a positive integer")
    except ValueError:
        mb.showwarning("Please enter a whole number for feedrate")

def open_in_candle(gcode_filename):
    os = sys.platform
    if os.startswith("win"):  #windows configuration
        print("windows config")
        subprocess.Popen([ r"C:\Program Files\Candle\candle.exe",
        gcode_filename
    ])
    elif os == "darwin":    #mac configuration
        print("macos config")
        # subprocess.Popen([
        #     "open",
        #     "-a",
        #     "Candle",
        #     gcode_filename
        # ])
        subprocess.run(['open', '-a', "Candle", gcode_filename], check=True)
        # candle_path = "/Applications/Candle.app/Contents/MacOS/Candle"
        # subprocess.Popen([candle_path, gcode_filename])
    elif os == "linux":
        print("linux config")
        subprocess.Popen(["/usr/bin/candle", gcode_filename])
    else:
        print("Unsupported operating system")

def open_in_octoprint(gcode_filename):
    # API endpoint for file uploads
    url = f"{OCTOPRINT_URL}api/files/local"
    headers = {"X-Api-Key": API_KEY}
    
    # 'select': 'true' tells OctoPrint to load it into the viewer immediately
    # 'print': 'false' ensures it doesn't start moving until you click 'Print'
    payload = {"select": "true", "print": "false"}

    with open(gcode_filename, 'rb') as f:
        files = {'file': f}
        response = requests.post(url, headers=headers, files=files, data=payload)

    if response.status_code == 201:
        logger.info("File uploaded and loaded successfully!")
    else:
        logger.error(f"Upload failed: {response.text}")

    webbrowser.open(OCTOPRINT_URL, autoraise=True, new=0)

def get_width():
    width = width_tb.get()
    if width == "" or not is_float(width):
        width=5
    metric = metric_option.get()
    logger.debug(f"Using metric: {metric}")
    return metric_to_mm_converter(width, metric)

def get_length():
    length = length_tb.get()
    if length == "" or not is_float(length):
        length=5
    metric = metric_option.get()
    logger.debug(f"Using metric: {metric}")
    return metric_to_mm_converter(length, metric)

def shape_clicked(event): #executed when shape from listbox is selected
    global shape_to_draw
    global shape_original_coords  # these are the coordinates before moving the shape on the canvas
    
    #### check whether textbox has an input or not ###
    shape = lb.get() #gets the text value of the selected shape
    if shape=="--":
        shape = RECTANGLE
        logger.info("No shape selected. Using rectangle as the default")

    canvas.delete("all") # delete previous drawing

    # Finding center of canvas
    canvas_center_x = CANVAS_W // 2   
    canvas_center_y = CANVAS_H // 2

    shape_width = get_width()
    shape_height = get_length()
    # logger.debug(f"Width input is a float?: {is_float(width)}, Length input is a float?: {is_float(length)}")

    # if width == "" or length == "" or not is_float(width) or not is_float(length):
    #     logger.info("Using default value of 5W x 5L")
    #     width = 5
    #     length = 5

    metric = metric_option.get()
    # logger.debug(f"Using metric: {metric}")
    # if metric == "Option 1": 
    #     logger.warning("Please select a metric")
    #     mb.showwarning("Warning!!", "Please select a metric")
    #     return
    # shape_width = metric_to_mm_converter(width, metric)
    # shape_height = metric_to_mm_converter(length, metric)
    logger.info(f"Shape Dimensions = {shape_width}W x {shape_height}L.")
    # print(f"type: {type(shape_width)}, {type(shape_height)}")

    # Depending on shape selected, the canvas will display the shape
    if shape == RECTANGLE:
        shape_to_draw = canvas.create_rectangle(0, 
                                                0, 
                                                shape_width, 
                                                shape_height, 
                                                outline="black") #for testing in cm; multiply by 10 for cm->mm
    elif shape == CIRCLE:
        shape_to_draw = canvas.create_oval(0, 
                                           0, 
                                           shape_width, 
                                           shape_height, 
                                           outline="black")  #for testing in cm; multiply by 10 for cm->mm
    elif shape == "Oval":
        pass
    
    shape_original_coords = canvas.coords(shape_to_draw)  #stores the coordinates of the shape before it's moved on the canvas
    print(shape_original_coords)

    # scales the shape to fit into the gui canvas
    scale_factor = min((CANVAS_W * 0.8) / shape_width, 
                       (CANVAS_H * 0.8) / shape_height)
    print(scale_factor)
    print((CANVAS_W * 0.8) / metric_to_mm_converter(shape_width, metric), 
                       (CANVAS_H * 0.8) / metric_to_mm_converter(shape_height, metric))
    canvas.scale(shape_to_draw, 0, 0, scale_factor, scale_factor)

    x1, y1, x2, y2 = canvas.bbox(shape_to_draw)

    #Finding center of the shape
    shape_center_x = (x1 + x2) / 2
    shape_center_y = (y1 + y2) / 2

    offset_x = canvas_center_x - shape_center_x
    offset_y = canvas_center_y - shape_center_y

    # moves the shape to the center of the canvas
    canvas.move("all", 
                offset_x, 
                offset_y
            )

    logger.debug(f"Shape ID: {shape_to_draw}")

    return shape_to_draw

def path_clicked(event=None): #executed when path from listbox is selected
    global path_file
    global original_paths
    global NUM_PASSES
    #checks that a shape is selected first
    if shape_to_draw==None:  
        mb.showwarning("Warning!!", "Please select a shape")
        return
    
    path_selected = path_lb.get() #curselection outputs a tuple of ints; takes the first element
    # path_selected = path_lb.get(selected_path) # gets the text associated with element
    logger.debug(f"Selected Path: {path_selected}")

    coords = canvas.coords(shape_to_draw) #gets coordinates of the drawn shape on the canvas
    x0, y0, x1, y1 = coords
    x_0, y_0, x_1, y_1 = shape_original_coords 

    #checks the shape that the user has selected
    selected_shape = lb.get()
    if selected_shape == RECTANGLE:
        poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])  #creates polygon object; necessary for shapely library; used for displaying in tkinter
        original_poly = Polygon([(x_0, y_0), (x_1, y_0), (x_1, y_1), (x_0, y_1)])  #used for accurate coordinates in Candle

    elif selected_shape == CIRCLE:
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        r = (x1 - x0) / 2
        poly = Point(cx, cy).buffer(r)  #buffer = radius; creates object necessary for shapely library; used for displaying in tkinter

        c_x = (x_0 + x_1) / 2
        c_y = (y_0 + y_1) / 2
        r = (x_1 - x_0) / 2
        original_poly = Point(c_x, c_y).buffer(r) #used for accurate coordinates in Candle

    numofpasses= NUM_PASSES
    original_paths = []

    # generates gcode depending on selected path
    # the paths variable is used to display the paths in gui/canvas; coordinates are offseted to position shape in the middle of the gui
    # the original paths variable is used to get accurate coordinates in Candle; coordinates start from the origin (0,0)
    if path_selected == SPIRAL:
        base = spiral_paths(original_poly, SPRAYER_WIDTH)
        for i in range(numofpasses):
            original_paths.extend(base)

            if i < numofpasses - 1:
                original_paths.append("DWELL")
        visual_path = spiral_paths(poly, SPRAYER_WIDTH*10)
        print(original_paths)
        path_file = "spiral.gcode"
        print("spiral path generated")
    elif path_selected == ZIGZAG:
        base = raster_paths_xdir(original_poly, SPRAYER_WIDTH)
        for i in range(numofpasses):
            original_paths.extend(base)

            if i < numofpasses - 1:
                original_paths.append("DWELL")
        visual_path = raster_paths(poly, SPRAYER_WIDTH*10)
        print(original_paths)
        path_file = "raster.gcode"
        print("raster path generated")
    elif path_selected == CROSSHATCH:
        base = crosshatch_paths(original_poly, SPRAYER_WIDTH)
        for i in range(numofpasses):
            original_paths.extend(base)

            if i < numofpasses - 1:
                original_paths.append("DWELL")
        visual_path = crosshatch_paths(poly, SPRAYER_WIDTH*10)
        print(original_paths)
        path_file = "crosshatch.gcode"
        print("crosshatch path generated")
    elif path_selected == ANGLED:
        base = angled_crosshatch_paths(original_poly, SPRAYER_WIDTH)
        for i in range(numofpasses):
            original_paths.extend(base)

            if i < numofpasses - 1:
                original_paths.append("DWELL")
        visual_path = angled_crosshatch_paths(poly, SPRAYER_WIDTH*10)
        path_file = "angledcrosshatch.gcode"
        print("angled crosshatch path generated")
    elif path_selected == ISOTROPIC: 
        base = isotropic_paths(original_poly, SPRAYER_WIDTH)
        for i in range(numofpasses):
            original_paths.extend(base)

            if i < numofpasses - 1:
                original_paths.append("DWELL")
        visual_path = isotropic_paths(poly, SPRAYER_WIDTH*10)
        path_file = "isotropic.gcode"
        print("isotropic path generated")
    elif path_selected == OFFSET_RASTER:
        original_paths = offset_raster_path(original_poly, SPRAYER_WIDTH, numofpasses)
        visual_path = raster_paths(poly, SPRAYER_WIDTH*10)
        print(original_paths)
        path_file = "offset.gcode"
        print("offset raster path generated")
    
    # Displays selected paths on the canvas
    canvas.delete("path_lines") #deletes previously traced path
    for path in visual_path:
        if path == "DWELL":
            continue
        canvas.create_line(path, fill="blue", tags="path_lines")


# =========================
# SETUP  & SHUTDOWN
# =========================
def background_setup(): # connects to arduino and connects printer to octoprint
    # OPEN the arduino serial connection once at the start (as-original -- zero-drift).
    # It is never written to today (no ser.write anywhere), so it emits no motion
    # bytes; if a raw servo write is ever added it MUST go through _motion_permitted().
    # On a Pi where OctoPrint holds the printer port this open simply fails and logs.
    try:
        ser = serial.Serial(ARDUINO_PORT, 250000)
        logger.info("Successfully  connected to Servo")
        time.sleep(2) # Wait for the reboot
    except:
        logger.error("Could not connect to arduino. Check the port name!")

    headers = {
        "X-Api-Key": API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "command": "connect",
        "printer_profile": "_default",
        "save": True,
        "autoconnect": True 
        }

    try:  # connect to printer after creating the server
        response = requests.post(f"{OCTOPRINT_URL}/api/connection", headers=headers, json=payload)
        if response.status_code == 204:
            logger.info("Successfully connected to printer")
        else:
            logger.warning("Printer was not connected")
    except Exception as e:
        logger.error(f"Failed to connect to OctoPrint/printer: {e}")


    # Startup homing (G28) runs only when talking to a LOOPBACK OctoPrint (Pi-local
    # or PC-virtual) -- exactly as the original did. Suppressed for a remote host so a
    # PC never homes a remote Pi's printer. Independent of ARM (zero-drift on the Pi).
    if not STARTUP_HOMING:
        logger.info("Startup homing suppressed (remote OctoPrint host); skipping G28.")
    else:
        payload = {"commands": ["G28 X Y"]}
        try:
            response = requests.post(f"{OCTOPRINT_URL}api/printer/command", headers=headers, json=payload)
            if response.status_code == 204:
                logger.info("Successfully sent jog command to OctoPrint!")
            else:
                logger.error(f"Error: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Failed to connect to OctoPrint / move machine: {e}")

    
    # update_gui_coordinates()
    
def on_closing():
    global ser
    try:
        # CLOSE connection when the window is closed
        ser.close()
    except:
        print("Serial is not connected, nothing to close\n")
    # root.destroy()
    # generate_button.config(state="disabled") 

def finish(): # Runs when the finish shape button is clicked
    global path_file, original_paths, CURRENT_X, CURRENT_Y

    if path_lb.get() == "--":  #checks that a shape is selected first
        mb.showwarning("Warning!!", "Please select a path")
        return
    print(path_file)
    path_clicked()
    write_gcode(path_file, original_paths)
    # CURRENT_X = 0.0
    # CURRENT_Y = 0.0
    logger.info(f"{path_file} generated")
    # path_file is written to the current working directory by write_gcode;
    # upload that same local file (previously a hardcoded Pi Linux path).
    open_in_octoprint(path_file)
    on_closing()




###################
# START OF PROGRAM#
###################

#### SETUP #####
if __name__ == "__main__":
    background_setup() #connects to arduino and octoprint server
# update_gui_coordinates()

ctk.set_default_color_theme("dark-blue")

root = ctk.CTk()
root.title("Draw Substrate Boundary")

root.grid_columnconfigure(0, weight=1)
root.grid_rowconfigure(0, weight=0) # Keeps tabview tight to the top
root.grid_rowconfigure(1, weight=1)

## TabView
tabview = ctk.CTkTabview(master=root)
tabview.grid(row=0, column=0, padx=0, pady=0, sticky="nw")

dim_tab = tabview.add("Dimensions")  # add tab at the end
extra_tab = tabview.add("Additional")  # add tab at the end

###### DIMENSIONS INPUT ######
dimheading = ctk.CTkLabel(
            dim_tab, 
            text="Dimensions", 
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("black", "white")
        )
dimheading.grid(row=0, column=0, padx=(4, 10), sticky="w")

dimheadingline = ctk.CTkFrame(
            dim_tab, 
            height=1, 
            # fg_color="#dbdbdb"
            fg_color=("black", "white")
        )
dimheadingline.grid(row=0, column=1, padx=(0, 5), pady=(4, 0), sticky="ew")

input_frame = ctk.CTkFrame(dim_tab, fg_color="transparent")
input_frame.grid(row=1, column=0, columnspan=2, padx=10, pady=0, sticky="ew")

width_label = ctk.CTkLabel(input_frame, text="Width:", font=ctk.CTkFont(size=12, weight="normal"))
width_label.grid(row=0, column=0, pady=2, sticky="w")
width_tb = ctk.CTkEntry(input_frame, height=5, width=100, placeholder_text="5")
width_tb.grid(row=0, column=1, padx=(100, 0), pady=5, sticky="e")

length_label = ctk.CTkLabel(input_frame, text="Length:", font=ctk.CTkFont(size=12, weight="normal"))
length_label.grid(row=1, column=0, pady=2, sticky="w")
length_tb = ctk.CTkEntry(input_frame, height=5, width=100, placeholder_text="5")
length_tb.grid(row=1, column=1, padx=(100, 0), pady=5, sticky="e")

unit_var = ctk.StringVar(value="mm")
metric_option = ctk.CTkSegmentedButton(
    input_frame, 
    values=["mm", "cm", "in"],
    variable=unit_var,
    dynamic_resizing=False,
    width=150,
    height=27
)
metric_option.grid(row=2, column=1, columnspan=2, pady=(7, 15), sticky="w")

###### SHAPE INPUT ######
shape_header_frame = ctk.CTkFrame(dim_tab, fg_color="transparent")
shape_header_frame.grid(row=2, column=0, columnspan=2, padx=0, pady=(0, 0), sticky="ew")
# shape_header_frame.grid_columnconfigure(1, weight=1)
shapeheading = ctk.CTkLabel(
            shape_header_frame, 
            text="Shape", 
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("black", "white")
        )
shapeheading.grid(row=0, column=0, columnspan=1, padx=(4, 10), sticky="w")

shapeheadingline = ctk.CTkFrame(
            shape_header_frame, 
            height=1, 
            # fg_color="#dbdbdb"
            fg_color=("black", "white")
        )
shapeheadingline.grid(row=0, column=1, padx=(0, 5), pady=(4, 0), sticky="ew")

shape_frame = ctk.CTkFrame(shape_header_frame, fg_color="transparent")
shape_frame.grid(row=4, column=0, columnspan=2, padx=10, pady=10, sticky="ew")

shape_label = ctk.CTkLabel(shape_frame, font=("Lexend", 12), text="Select a shape:")
shape_label.grid(row=0, column=0, pady=(0, 0), padx=(0, 30))
lb = ctk.CTkOptionMenu(shape_frame, values=["--", "Rectangle", "Circle"],
                       height=20, 
                       command=shape_clicked)
lb.grid(row=0, column=1)

###### PATH INPUT ######
path_header_frame = ctk.CTkFrame(dim_tab, fg_color="transparent")
path_header_frame.grid(row=3, column=0, columnspan=2, padx=0, pady=(0, 0), sticky="ew")
pathheading = ctk.CTkLabel(
            path_header_frame, 
            text="Path", 
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("black", "white")
        )
pathheading.grid(row=0, column=0, columnspan=1, padx=(4, 10), sticky="w")

pathheadingline = ctk.CTkFrame(
            path_header_frame, 
            height=1, 
            # fg_color="#dbdbdb"
            fg_color=("black", "white")
        )
pathheadingline.grid(row=0, column=1, padx=(0, 5), pady=(4, 0), sticky="ew")

path_frame = ctk.CTkFrame(path_header_frame, fg_color="transparent")
path_frame.grid(row=6, column=0, columnspan=2, padx=10, pady=10, sticky="ew")

path_label = ctk.CTkLabel(path_frame, font=("Lexend", 12), text="Select a path:")
path_label.grid(row=0, column=0, pady=(0, 0), padx=(0, 30))
path_lb = ctk.CTkOptionMenu(path_frame, 
                            values=["--", SPIRAL, CROSSHATCH, ZIGZAG, ANGLED, ISOTROPIC, OFFSET_RASTER],
                            command=path_clicked,
                            height=20)
path_lb.grid(row=0, column=1)

###### SERVO DEGREE SELECTION #######
servo_header_frame = ctk.CTkFrame(dim_tab, fg_color="transparent")
servo_header_frame.grid(row=4, column=0, columnspan=2, padx=0, pady=(0, 0), sticky="ew")
servoheading = ctk.CTkLabel(
            servo_header_frame, 
            text="Trigger", 
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=("black", "white")
        )
servoheading.grid(row=0, column=0, columnspan=1, padx=(4, 10), sticky="w")

servoheadingline = ctk.CTkFrame(
            servo_header_frame, 
            height=1, 
            # fg_color="#dbdbdb"
            fg_color=("black", "white")
        )
servoheadingline.grid(row=0, column=1, padx=(0, 0), pady=(4, 0), sticky="ew")

servo_frame = ctk.CTkFrame(servo_header_frame, fg_color="transparent")
servo_frame.grid(row=8, column=0, columnspan=2, padx=(15,0), pady=10, sticky="ew")

# Textbox for servo degrees 
servoDegreetb = ctk.CTkEntry(servo_frame, width=50, placeholder_text="0")
servoDegreetb.grid(row=0, column=0, pady=(0, 0), padx=(0, 50), sticky="e")
ctk.CTkButton(servo_frame, text="Move servo", command=move_servo).grid(row=0, column=1)

###### SERVO DEGREE SELECTION #######


###### NUM PASSES SELECTION #######
passes_frame = ctk.CTkFrame(extra_tab, fg_color="transparent")
passes_frame.grid(row=0, column=0, columnspan=2, padx=(5,0), pady=(0, 0), sticky="ew")

passes_label = ctk.CTkLabel(passes_frame, font=("Lexend", 13), text="Num. of Passes: ")
passes_label.grid(row=0, column=0, padx=(5, 45), pady=(0, 0))

numPassestb = ctk.CTkEntry(passes_frame, width=80, height=7)
numPassestb.insert(0, str(NUM_PASSES))   # prefill with default
numPassestb.grid(row=0, column=1, padx=(40, 0))

ctk.CTkButton(passes_frame, width=45, text="Set Passes", command=setNumPasses).grid(row=1,
                                                                                     column=1,  
                                                                                     padx=(40,0),
                                                                                     pady=(5, 15),
                                                                                     columnspan=2)
###### NUM PASSES SELECTION #######

###### SPRAYER WIDTH SELECTION #######
width_frame = ctk.CTkFrame(extra_tab, fg_color="transparent")
width_frame.grid(row=1, column=0, columnspan=2, padx=(5,0), pady=(10, 0), sticky="ew")

width_input_label = ctk.CTkLabel(width_frame, font=("Lexend", 13), text="Sprayer Width (mm): ")
width_input_label.grid(row=0, column=0, padx=(5, 22), pady=(0, 0))

sprayerWidthtb = ctk.CTkEntry(width_frame,  width=80, height=7)
sprayerWidthtb.insert(0, str(SPRAYER_WIDTH))   # prefill with default
sprayerWidthtb.grid(row=0, column=1, padx=(35, 0))

ctk.CTkButton(width_frame, width=45, text="Set Width", command=setSprayerWidth).grid(row=1, 
                                                                                     column=1,
                                                                                     padx=(35,0),
                                                                                     pady=(5, 15),
                                                                                     columnspan=2)
###### SPRAYER WIDTH SELECTION #######

###### OVERRUN SELECTION #######
overrun_frame = ctk.CTkFrame(extra_tab, fg_color="transparent")
overrun_frame.grid(row=2, column=0, columnspan=2, padx=(5,0), pady=(10, 0), sticky="ew")

overrun_label = ctk.CTkLabel(overrun_frame, font=("Lexend", 13), text="Overrun (mm):")
overrun_label.grid(row=0, column=0, padx=(5, 50), pady=(0, 0))

overruntb = ctk.CTkEntry(overrun_frame, width=80, height=7)
overruntb.insert(0, str(OVERRUN))   # prefill with default
overruntb.grid(row=0, column=1, padx=(50, 0))

ctk.CTkButton(overrun_frame, width=45, text="Set Overrun", command=setOverrun).grid(row=1, 
                                                                                    column=1,
                                                                                    padx=(48,0),
                                                                                    pady=(5, 15),
                                                                                    columnspan=2)
###### OVERRUN SELECTION #######

###### FEEDRATE SELECTION #######
feedrate_frame = ctk.CTkFrame(extra_tab, fg_color="transparent")
feedrate_frame.grid(row=3, column=0, columnspan=2, padx=(5,0), pady=(10, 0), sticky="ew")

feedrate_label = ctk.CTkLabel(feedrate_frame, font=("Lexend", 13), text="Feedrate (mm/min):")
feedrate_label.grid(row=0, column=0, padx=(5, 25), pady=(0, 0))

feedratetb = ctk.CTkEntry(feedrate_frame, width=80, height=7)
feedratetb.insert(0, str(FEEDRATE))   # prefill with default
feedratetb.grid(row=0, column=1, padx=(45, 10))

ctk.CTkButton(feedrate_frame, width=45, text="Set Feedrate", command=setFeedrate).grid(row=1, 
                                                                                       column=1,
                                                                                       padx=(40,0),
                                                                                       pady=(5, 15),
                                                                                       columnspan=2)
###### FEEDRATE SELECTION #######

# ###### HEIGHT SELECTION #######
# height_frame = ctk.CTkFrame(extra_tab, fg_color="transparent")
# height_frame.grid(row=4, column=0, columnspan=2, padx=(15,0), pady=(0, 0), sticky="ew")

# height_label = ctk.CTkLabel(height_frame, text="Z height:")
# height_label.grid(row=0, column=0, padx=(0, 10), pady=(0, 0))

# heightEntry = ctk.CTkEntry(height_frame, width=15)
# heightEntry.grid(row=0, column=1)
# # tk.Button(control_frame, text="Move servo", command=move_servo).grid(row=10, column=0)
# ###### HEIGHT SELECTION #######

###### CONTINUOUS SPRAY TOGGLE #######
continuous_spray_var = tk.BooleanVar(value=False)
continuous_frame = ctk.CTkFrame(extra_tab, fg_color="transparent")
continuous_frame.grid(row=4, column=0, columnspan=2, padx=(5, 0), pady=(15, 0), sticky="ew")
continuous_spray_checkbox = ctk.CTkCheckBox(
    continuous_frame,
    text="Continuous Spray",
    variable=continuous_spray_var,
    onvalue=True,
    offvalue=False,
)
continuous_spray_checkbox.grid(row=0, column=0, padx=(5, 0), pady=(0, 0), sticky="w")
###### CONTINUOUS SPRAY TOGGLE #######

generate_button = ctk.CTkButton(root, text="Generate!", command=finish)
generate_button.grid(row=1, column=0, pady=(0, 10))

###### LOG / FEEDBACK BOX #######
log_frame = tk.Frame(root)
log_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=5)

tk.Label(log_frame, text="Log:", font=("Lexend", 12)).pack(anchor="w")

log_scrollbar = tk.Scrollbar(log_frame)
log_scrollbar.pack(side="right", fill="y")

log_box = tk.Text(log_frame, height=8, state="disabled", yscrollcommand=log_scrollbar.set)
log_box.pack(fill="x")
log_scrollbar.config(command=log_box.yview)

gui_handler = TextLogHandler(log_box)
gui_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(gui_handler)
###### LOG / FEEDBACK BOX #######

###### TERMINAL PANEL (OctoPrint comm) ######
term_frame = tk.Frame(root)
term_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 8))

_term_header = tk.Frame(term_frame)
_term_header.pack(fill="x")
tk.Label(_term_header, text="Terminal (OctoPrint comm):", font=("Lexend", 12)).pack(side="left", anchor="w")
terminal_autoscroll_var = tk.BooleanVar(value=True)
tk.Checkbutton(_term_header, text="Autoscroll", variable=terminal_autoscroll_var).pack(side="right")

_term_scroll = tk.Scrollbar(term_frame)
_term_scroll.pack(side="right", fill="y")
terminal_log = tk.Text(term_frame, height=8, state="disabled", yscrollcommand=_term_scroll.set)
terminal_log.pack(fill="x")
_term_scroll.config(command=terminal_log.yview)

_term_input_row = tk.Frame(term_frame)
_term_input_row.pack(fill="x", pady=(4, 0))
terminal_send_button = ctk.CTkButton(_term_input_row, text="Send", width=70, command=lambda: _terminal_send())
terminal_send_button.pack(side="right", padx=(6, 0))
terminal_input = tk.Text(_term_input_row, height=2)
terminal_input.pack(side="left", fill="x", expand=True)
terminal_input.bind("<Return>", lambda e: (_terminal_send(), "break")[1])   # Enter sends
terminal_input.bind("<Shift-Return>", lambda e: None)                        # Shift+Enter = newline
terminal_input.bind("<Up>", lambda e: _terminal_history_nav(-1))
terminal_input.bind("<Down>", lambda e: _terminal_history_nav(1))
###### TERMINAL PANEL ######

# ##### White Canvas above selectors
canvas = tk.Canvas(root, width=CANVAS_W, height=CANVAS_H, bg="white")
canvas.grid(row = 0, column=1) # Put the Canvas in row 0, col 0 
# #####

##### Jog Panel #######
jog_panel = ctk.CTkFrame(root, fg_color="transparent", width=290)
jog_panel.grid_propagate(False)
jog_panel.grid(row=0, column=2, padx=(20, 20), pady=(20, 0), sticky="nswe")
for _c in range(3):
    jog_panel.grid_columnconfigure(_c, weight=1)

# --- Home row ---
home_xy_button = ctk.CTkButton(jog_panel, text="Home XY", width=80, height=28,
                               command=lambda: _do_home(["x", "y"]))
home_xy_button.grid(row=0, column=0, columnspan=2, pady=(6, 6), padx=2, sticky="ew")
home_all_button = ctk.CTkButton(jog_panel, text="Home All", width=80, height=28,
                                command=lambda: _do_home(["x", "y", "z"]))
home_all_button.grid(row=0, column=2, pady=(6, 6), padx=2, sticky="ew")

# --- XY pad (cols 0-2) + Z column (col 3) ---
ybutton_top = ctk.CTkButton(jog_panel, text="▲", width=46, height=40, command=lambda: _do_jog(dy=+1))
ybutton_top.grid(row=1, column=1, pady=(4, 2))
xbutton_left = ctk.CTkButton(jog_panel, text="◀", width=46, height=40, command=lambda: _do_jog(dx=-1))
xbutton_left.grid(row=2, column=0)
xbutton_right = ctk.CTkButton(jog_panel, text="▶", width=46, height=40, command=lambda: _do_jog(dx=+1))
xbutton_right.grid(row=2, column=2)
ybutton_bottom = ctk.CTkButton(jog_panel, text="▼", width=46, height=40, command=lambda: _do_jog(dy=-1))
ybutton_bottom.grid(row=3, column=1, pady=(2, 4))

zbutton_top = ctk.CTkButton(jog_panel, text="Z ▲", width=46, height=34, command=lambda: _do_jog(dz=+1))
zbutton_top.grid(row=1, column=3, padx=(10, 0), pady=(4, 2))
zbutton_bottom = ctk.CTkButton(jog_panel, text="Z ▼", width=46, height=34, command=lambda: _do_jog(dz=-1))
zbutton_bottom.grid(row=3, column=3, padx=(10, 0), pady=(2, 4))

# --- Step selector (0.1 / 1 / 10 mm) ---
jog_step_var = ctk.StringVar(value="1 mm")
jog_step_option = ctk.CTkSegmentedButton(jog_panel, values=["0.1 mm", "1 mm", "10 mm"],
                                         variable=jog_step_var, dynamic_resizing=False, height=28)
jog_step_option.grid(row=4, column=0, columnspan=4, pady=(10, 4), padx=2, sticky="ew")

# --- Jog feedrate ---
feed_row = ctk.CTkFrame(jog_panel, fg_color="transparent")
feed_row.grid(row=5, column=0, columnspan=4, pady=(2, 4), padx=2, sticky="w")
ctk.CTkLabel(feed_row, text="Jog feedrate (mm/min):").grid(row=0, column=0, padx=(0, 6))
jog_feedrate_entry = ctk.CTkEntry(feed_row, width=70)
jog_feedrate_entry.insert(0, "1500")
jog_feedrate_entry.grid(row=0, column=1)

# --- Position readout (from M114 via the push socket; "—" when unknown) ---
x_coord_str = ctk.StringVar(value="X: —")
y_coord_str = ctk.StringVar(value="Y: —")
z_coord_str = ctk.StringVar(value="Z: —")
servo_angle_str = ctk.StringVar(value="Servo (commanded): —")
readout = ctk.CTkFrame(jog_panel, fg_color="transparent")
readout.grid(row=6, column=0, columnspan=4, pady=(6, 2))
ctk.CTkLabel(readout, textvariable=x_coord_str, font=ctk.CTkFont(size=14, weight="bold"), text_color="#3b8ed0").grid(row=0, column=0, padx=6)
ctk.CTkLabel(readout, textvariable=y_coord_str, font=ctk.CTkFont(size=14, weight="bold"), text_color="#3b8ed0").grid(row=0, column=1, padx=6)
ctk.CTkLabel(readout, textvariable=z_coord_str, font=ctk.CTkFont(size=14, weight="bold"), text_color="#3b8ed0").grid(row=0, column=2, padx=6)
ctk.CTkLabel(readout, textvariable=servo_angle_str, font=ctk.CTkFont(size=13, weight="bold"), text_color="#ff8f00").grid(row=1, column=0, columnspan=3, pady=(4, 0))

panel_note = ctk.CTkLabel(jog_panel, text="Jog via OctoPrint printhead API; greys out when busy/offline")
panel_note.grid(row=7, column=0, columnspan=4, pady=(4, 0))

###### MACHINE CONTROLS: ARM + one-click Print / Pause / Cancel ######
_arm_var = tk.BooleanVar(value=False)
arm_switch = ctk.CTkSwitch(jog_panel, text="ARM MOTION", variable=_arm_var,
                           command=lambda: _set_armed(_arm_var.get()),
                           progress_color="#e53935", button_color="#e53935",
                           font=ctk.CTkFont(size=14, weight="bold"))
arm_switch.grid(row=8, column=0, columnspan=4, pady=(10, 2))
print_button = ctk.CTkButton(jog_panel, text="Print — select a file", command=lambda: _do_print())
print_button.grid(row=9, column=0, columnspan=4, padx=2, pady=(4, 2), sticky="ew")
_pc_row = ctk.CTkFrame(jog_panel, fg_color="transparent")
_pc_row.grid(row=10, column=0, columnspan=4, pady=(2, 4))
pause_button = ctk.CTkButton(_pc_row, text="Pause", width=70, command=lambda: _do_pause())
pause_button.grid(row=0, column=0, padx=3)
cancel_button = ctk.CTkButton(_pc_row, text="Cancel", width=70, fg_color="#b71c1c",
                              hover_color="#7f0000", command=lambda: _do_cancel())
cancel_button.grid(row=0, column=1, padx=3)
###### MACHINE CONTROLS ######

# Widgets greyed out unless Operational & idle (managed in update_gui_coordinates)
_jog_widgets = [home_xy_button, home_all_button, xbutton_left, xbutton_right,
                ybutton_top, ybutton_bottom, zbutton_top, zbutton_bottom,
                jog_step_option, jog_feedrate_entry]

_plink.start()            # begin the OctoPrint push-socket link
update_gui_coordinates()  # start readout + gating loop
update_marker()           # start the live airbrush marker on the canvas
_terminal_drain()         # start mirroring OctoPrint's comm stream into the terminal
##### Jog Panel #######

if __name__ == "__main__":
    root.mainloop()

# =========================
# GEOMETRY
# =========================
# print(points)
# polygon = Polygon(points)


# =========================
# RUN TEST
# =========================
# default_rect_path_generator()
# default_circle_path_generator()

# spiral = spiral_paths(polygon, SPRAYER_WIDTH)
# raster = raster_paths(polygon, SPRAYER_WIDTH)
# cross  = crosshatch_paths(polygon, SPRAYER_WIDTH)

# write_gcode("spiral.nc", spiral)
# write_gcode("raster.nc", raster)
# write_gcode("crosshatch.nc", cross)

# print("Generated:")
# print(" - spiral.nc")
# print(" - raster.nc")
# print(" - crosshatch.nc")
# print("Open in Candle to preview & run")

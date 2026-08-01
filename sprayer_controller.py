import tkinter as tk
import customtkinter as ctk
from tkinter import ttk
from tkinter import messagebox as mb
import sys
import math
import logging
import webbrowser
import requests
import subprocess
import numpy as np
import serial
import time
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
# Connection settings (Arduino port, OctoPrint URL/port, API key) live in
# config.py and are selected by the UCEI_TARGET env var (default LOCAL, or PI).
# For LOCAL the API key is read from config_local.py (git-ignored) so keys are
# never committed.
from config import (ARDUINO_PORT, OCTOPRINT_PORT, OCTOPRINT_URL, API_KEY,
                    ALLOW_STARTUP_MOTION, CONFIRM_MOTION, TARGET)
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger()

class TextLogHandler(logging.Handler):
    """Custom logging handler that writes log messages into a Tkinter Text widget"""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        self.text_widget.config(state="normal")
        self.text_widget.insert("end", msg + "\n")
        self.text_widget.see("end")
        self.text_widget.config(state="disabled")

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

        E=0 #needed for extrusion. octoprint is for 3d printers so if extrusion isn't mentioned, it thinks that nothing is happening

        for path in paths:    
            print("path: ",path)
            if path == "DWELL":
                f.write("M280 P0 S0\n") # Spray OFF
                f.write("G0 X0 Y0\n") # 1. Park the nozzle at origin to avoid heat
                f.write("G4 S5\n")    # 2. Dwell for 5 seconds
                continue               # 3. Move to the next pass (45 degrees)    
            x0, y0 = path[0]
            # x_end, y_end = path[-1]

            f.write(f"G0 X{x0:.2f} Y{y0:.2f}\n")

            # Spray OFF
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

            # Spray ON
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
def _motion_allowed(action_desc):
    """Safety gate for interactive motion/actuation commands.

    Returns True if the command may be sent to the printer. In profiles that
    require confirmation (CONFIRM_MOTION, e.g. PI / real hardware) the operator
    is prompted first and False is returned if they decline.
    """
    if not CONFIRM_MOTION:
        return True
    allowed = mb.askyesno(
        f"Confirm motion  [{TARGET}]",
        f"Send this command to the REAL machine?\n\n{action_desc}\n\n"
        "Make sure the work area is clear before proceeding.",
        icon="warning",
    )
    if not allowed:
        logger.warning(f"[{TARGET}] Motion command cancelled by operator: {action_desc}")
    return allowed


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

def update_gui_coordinates():
    # print(x_coord_str)
    x_coord_str.set(f"X: {CURRENT_X:.1f} mm")
    y_coord_str.set(f"Y: {CURRENT_Y:.1f} mm")
    z_coord_str.set(f"Z: {CURRENT_Z:.1f} mm")
    
    # Refresh every 250 milliseconds
    root.after(500, update_gui_coordinates)

def jog_machine(event):
    global CURRENT_X, CURRENT_Y, CURRENT_Z
    displacement = jog_option.get()
    logger.debug(f"User clicked {displacement} and {event}")
    if not _motion_allowed(f"Jog {event} by {displacement}"):
        return
    commands = []
    headers = {
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json"
        }
    if event=="MOVE":
        commands = [f"G0 X{CURRENT_X} Y{CURRENT_Y}"]
    elif event=="HOME":
        commands = [f"G10 L2 P1 X{CURRENT_X} Y{CURRENT_Y}", "G54"]
    elif event[-1] == "-":
        commands=["G21", "G91", f"G0 {event}{displacement[:-2]}", "G90"]
        if event[0]=="X": CURRENT_X -= float(displacement[:-2])
        elif event[0]=="Z": CURRENT_Z -= float(displacement[:-2])
        else: CURRENT_Y -= float(displacement[:-2])
    else:
        commands=["G21", "G91", f"G0 {event[:-1]}{displacement[:-2]}", "G90"]
        if event[0]=="X": CURRENT_X += float(displacement[:-2])
        elif event[0]=="Z": CURRENT_Z += float(displacement[:-2])
        else: CURRENT_Y += float(displacement[:-2])

    print(CURRENT_X, CURRENT_Y)
    payload = {"commands": commands}

    try:
        response = requests.post(f"{OCTOPRINT_URL}api/printer/command", headers=headers, json=payload)
        if response.status_code == 204:
            logger.info("Successfully sent jog command to OctoPrint!")
        else:
            logger.error(f"Error: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Failed to connect to OctoPrint / move machine: {e}")

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
    # OPEN arduino connection once at the start
    if ARDUINO_PORT is None:
        logger.info("No Arduino port configured for this target (UCEI_TARGET); skipping serial connection.")
    else:
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


    # Safety gate: only home (G28) on startup when the active profile allows it.
    # PI (real hardware) sets allow_startup_motion=False so nothing moves on launch.
    if not ALLOW_STARTUP_MOTION:
        logger.warning(f"[{TARGET}] Startup motion disabled (allow_startup_motion=False); "
                       "skipping G28 homing on startup.")
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

# ##### White Canvas above selectors
canvas = tk.Canvas(root, width=CANVAS_W, height=CANVAS_H, bg="white")
canvas.grid(row = 0, column=1) # Put the Canvas in row 0, col 0 
# #####

##### Jog Panel #######
jog_panel = ctk.CTkFrame(root, fg_color="transparent", width=270)
jog_panel.grid_propagate(False)
jog_panel.grid(row=0, column=2, padx=(20, 20), pady=(30, 0), sticky="nswe")

jog_panel.grid_columnconfigure(0, weight=1)
jog_panel.grid_columnconfigure(1, weight=1)
jog_panel.grid_columnconfigure(2, weight=1)

jog_panel.grid_rowconfigure(0, weight=1)  # Top buffer spring
jog_panel.grid_rowconfigure(5, weight=1)  # Bottom buffer spring

# jogpanel_label = ctk.CTkLabel(jog_panel, font=ctk.CTkFont(size=15, weight="bold"), text="Work Home Coordinates:")
# jogpanel_label.grid(row=0, columnspan=3, column=0)

workhome_move = ctk.CTkButton(jog_panel, text="Current Work Home", width=30, height=30, command=lambda: jog_machine("MOVE"))
workhome_move.grid(row=0, column=0, columnspan=3, pady=(20), padx=(0, 0))

xbutton_left = ctk.CTkButton(jog_panel, text="◀", width=40, height=40, command=lambda: jog_machine("X-"))
xbutton_left.grid(row=2, column=0, pady=(20), sticky="ns")
xbutton_right = ctk.CTkButton(jog_panel, text="▶", width=40, height=40, command=lambda: jog_machine("X+"))
xbutton_right.grid(row=2, column=2, pady=(20))

ybutton_top = ctk.CTkButton(jog_panel, text="▲", width=40, height=40, command=lambda: jog_machine("Y+"))
ybutton_top.grid(row=1, column=1, padx=(20), pady=(0, 0))
ybutton_bottom = ctk.CTkButton(jog_panel, text="▼", width=40, height=40, command=lambda: jog_machine("Y-"))
ybutton_bottom.grid(row=3, column=1, padx=(20), pady=(0, 0))

zbutton_top = ctk.CTkButton(jog_panel, text="▲", width=40, height=40, command=lambda: jog_machine("Z+"))
zbutton_top.grid(row=1, column=3, padx=(35, 0), pady=(0, 0))
zbutton_bottom = ctk.CTkButton(jog_panel, text="▼", width=40, height=40, command=lambda: jog_machine("Z-"))
zbutton_bottom.grid(row=3, column=3, padx=(35, 0), pady=(0, 0))

workhome_button = ctk.CTkButton(jog_panel, font=("Lexend", 25), text="⌂", width=50, height=50, command=lambda: jog_machine("HOME"))
workhome_button.grid(row=2, column=1, pady=(0, 0))

button_var = ctk.StringVar(value="1mm")
jog_option = ctk.CTkSegmentedButton(
    jog_panel, 
    values=["1mm", "10mm", "100mm"],
    variable=button_var,
    dynamic_resizing=False,
    width=170,
    height=30
)
jog_option.grid(row=4, column=0, columnspan=3, padx=(10, 0), pady=(20, 0), sticky="w")

x_coord_str = ctk.StringVar(value="X: 0.0 mm")
y_coord_str = ctk.StringVar(value="Y: 0.0 mm")
z_coord_str = ctk.StringVar(value="Z: 0.0 mm")

x_display = ctk.CTkLabel(jog_panel, textvariable=x_coord_str, font=ctk.CTkFont(size=14, weight="bold"), text_color="#3b8ed0")
x_display.grid(row=5, column=0, columnspan=2, padx=(0, 40))

y_display = ctk.CTkLabel(jog_panel, textvariable=y_coord_str, font=ctk.CTkFont(size=14, weight="bold"), text_color="#3b8ed0")
y_display.grid(row=5, column=1, columnspan=2, padx=(30,0))

z_display = ctk.CTkLabel(jog_panel, textvariable=z_coord_str, font=ctk.CTkFont(size=14, weight="bold"), text_color="#3b8ed0")
z_display.grid(row=5, column=2, columnspan=2, padx=(30,0))
update_gui_coordinates()

panel_note = ctk.CTkLabel(jog_panel, text="Click ⌂ to set the work home!")
panel_note.grid(row=6, columnspan=3, column=0, pady=(5, 0))
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

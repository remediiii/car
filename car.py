import I2C_LCD_driver, obd, time, math, threading, os, datetime
from obd import OBDStatus

"""
Running down the list of what this script does:
 - On startup, checks adapter status, and won't proceed untli the car is detected to have the ignition on so we can collect real data.
 - Once the car is detected, the first thing we'll do is dump all supported PIDs as well as grabbing a sample pool of data from each PID into a folder
   named after the car's VIN. Ideally, this script can be ran on several cars, but for my purposes it's explicitly tuned for my own Accord.
 - We then create a folder named trips, and open a file for writing there. Ideally these files would be named after a true timestamp but the
   Pi doesn't have a realtime clock, and we don't expect internet access. This file will infrequently have trip data 
   written to it, such as runtime, recorded aMPG, and fuel level.
 - We then spin up a few threads, namely obd_worker and mpg_worker. obd_worker will asyncronously collect all neccessary data at all times, while mpg_worker
   will continue aMPG calculations in the background.
 - Finally, we get to the main loop, which is nothing more than a few simple state checks and LCD display commands.
 - Every five loops, we write information to our trips file.


Plans for the future:
 - Ideally the Pi would be rewired to handle shutdowns more safely. Right now, we expect power to be cut out at any
moment, and so we keep IO operations to a minimum. In the future, we should use a buck converter and an add-a-circuit fuse to
hook into the car's fusebox, and let us detect when the car is turned off.
 - Okay so apparently python-OBD supports asyncronous calls within its library. Maybe we rewrite this again later?
 - Serve some data over a simple http server. Having a web interface to show data on a phone connected to the Pi's hotspot
 could be useful for adjusting settings for what to display on the LCD, or to show graphs of data collected over time.
 """

###
# Values specific for 2009 Honda Accord EX-L 2.4L 4cyl
GEAR_RATIOS = {"1": 2.652, "2": 1.614, "3": 1.082, "4": 0.773, "5": 0.566}
FINAL_DRIVE = 4.44
TIRE_DIAMETER = 25.8583  # P225/50 R17
###

TIRE_CIRCUMFERENCE = TIRE_DIAMETER * math.pi
GASOLINE_DENSITY = 6.1738  # pounds per gallon

lcd = I2C_LCD_driver.lcd()

state = {
    "speed": None,
    "rpm": None,
    "maf": None,
    "equiv_ratio": None,
    "fuel_level": None,
    "coolant_temp": None,
    "runtime": None,
    "sample_id": 0,  # Used later to determine sample freshness
    "vin": None,
    "gear": None,
    # These values should never exceed 99.99 due to min() in mpg_worker().
    "impg": None,
    "ampg": None,
}

lcd_lock = threading.Lock()
file_lock = threading.Lock()
lock = threading.Lock()
stop_event = threading.Event()


def log(log):
    print(f"[{datetime.datetime.now()}] {log}")


def lcd_msg(l1=None, l2=None, clear_lcd=False, setup=False):
    """Clears the LCD (if set) and displays up to lines of text. Holds lock on LCD processing."""
    with lcd_lock:
        if clear_lcd:
            lcd.lcd_clear()
        for line, message in enumerate((l1, l2), start=1):
            if message is not None and setup is False:
                lcd.lcd_display_string(
                    str(message)[:14], line
                )  # we reserve some space for the gear display
            else:
                lcd.lcd_display_string(str(message)[:16], line)


def setup(adapter):
    """
    This does a few things. First, query the adapter to get the VIN number, which is used to create a folder to hold the corresponding dump. This is an
    attempt to make this cross-carpatible. Next, read and dump every code the car says it supports. Last, get a sample pool of data. This
    should be run before doing ANY work, as it'll conflict with the obd_worker thread. Returns file.
    """
    state["vin"] = str(adapter.query(obd.commands.VIN).value.decode("utf-8"))
    vin_dir = state["vin"]

    # Create a folder for the car's data and move into it for data collection
    if not os.path.exists(vin_dir):
        lcd_msg("New VIN detected", "Setting up...", setup=True)
        os.mkdir(vin_dir)
        os.chdir(vin_dir)
        # Dump the car's supported commands to an external file
        commands = sorted(adapter.supported_commands, key=str)
        with open("supported_commands.txt", "w") as f:
            for command in commands:
                f.write(f"{command}\n")

        # Dump sample data of data for every PID the car says it supports
        with open("sample_dump.txt", "w") as f:
            for command in commands:
                try:
                    response = adapter.query(command)

                    f.write("===========" + "\n")
                    f.write(f"Command : {command.name}\n")
                    f.write(f"Value   : {response.value}\n")
                    f.write(f"Units   : {response.unit}\n")
                    f.write(f"Raw     : {response}\n")

                except Exception as e:
                    f.write("===========" + "\n")
                    f.write(f"Command : {command.name}\n")
                    f.write(f"ERROR   : {e}\n")

    else:
        log(f"Data for VIN {state['vin']} already exists. Skipping dump.")
        os.chdir(vin_dir)

    # Create a folder to hold trip data
    if not os.path.exists("trips"):
        os.mkdir("trips")

    file_increment = 1
    while True:
        file_path = "trips/trip-%s.csv" % file_increment
        if not os.path.exists(file_path):
            break
        file_increment += 1

    with open(file_path, "w") as f:
        log(f"Writing to {file_path}")
        f.write("runtime,ampg,fuel_level\n")

    return file_path


def gear_worker():
    """THREAD: Calculates current gear based off of vehicle speed and RPM. Unusually, we will also use this thread to
    write to the LCD. This is probably bad? We're holding LCD lock now, so it should be okay.
    Gear display will show up as it's own single character in the first row, last column of the display.
    This isn't very accurate. Just for fun. Can we make this better?"""
    while True:
        speed = state["speed"]
        rpm = state["rpm"]
        if speed is None or rpm is None or speed <= 5:
            with lcd_lock:
                lcd.lcd_display_string("G?", 1, 14)
        else:
            wheel_rpm = speed * 63360 / (TIRE_CIRCUMFERENCE * 60)

            prediction = float("inf")
            for gear, ratio in GEAR_RATIOS.items():
                expected_rpm = wheel_rpm * ratio * FINAL_DRIVE
                error = abs(rpm - expected_rpm)

                if error < prediction and error < 200:
                    prediction = error
                    state["gear"] = gear
                else:
                    with lcd_lock:
                        lcd.lcd_display_string("G?", 1, 14)
            with lcd_lock:
                lcd.lcd_display_string(f"G{state["gear"]}", 1, 14)

        time.sleep(0.5)


def obd_worker():
    """THREAD: Query the OBD adapter for all data we want to read (speed, rpm, maf, equiv, fuel level, runtime), and
    update the global state dict with raw values. The adapter can only receive one query at a time, so we need
    to lock this, as well as ensure we're not querying the adapter elsewhere. Increment sample_id.
    """
    while not stop_event.is_set():
        with lock:
            for key, getter in (
                ("speed", get_speed),
                ("rpm", get_rpm),
                ("maf", get_maf),
                ("equiv_ratio", get_equiv_ratio),
                ("fuel_level", get_fuel_level),
                ("coolant_temp", get_coolant_temp),
                ("runtime", get_runtime),
            ):
                state[key] = getter()
            state["sample_id"] += 1
        time.sleep(0.5)  # Adjust sleep as needed


def mpg_worker():
    """THREAD: Calculate instant MPG based on speed, maf, and equiv ratio values. Compare sample IDs
    to ensure that data is only calculated when samples are guaranteed fresh.

    aMPG is currently not calculated correctly. It calculates with all accumulated iMPG values, but not
    ones at rest. OBD cannot show distance travelled.
    """
    ampg_sample_count = 0
    last_sample_id = 0
    while not stop_event.is_set():
        with lock:
            if state["sample_id"] != last_sample_id:
                last_sample_id = state["sample_id"]
                if (
                    state["speed"] is not None
                    and state["speed"] > 0
                    and state["maf"] is not None
                    and state["maf"] > 0
                    and state["equiv_ratio"] is not None
                    and state["equiv_ratio"] > 0
                ):
                    # formula from https://manuals.plus/m/8f08573961e7c5e83133532cdd853b80026fa4487393a7c52304287d758e9f39
                    impg = (
                        (14.7 / state["equiv_ratio"])
                        * GASOLINE_DENSITY
                        * 454
                        * state["speed"]
                    ) / (3600 * state["maf"])
                    impg = min(impg, 99.99)
                    state["impg"] = impg
                    ampg_sample_count += 1
                    if state["ampg"] is None:
                        state["ampg"] = impg
                    else:
                        state["ampg"] += (impg - state["ampg"]) / ampg_sample_count
                    state["ampg"] = min(state["ampg"], 99.99)
                else:
                    state["impg"] = None
        time.sleep(0.5)


# Getters
# These all return the raw values of each query, no units included. If a query fails for any reason, None is returned.
def get_speed():  # in MPH
    try:
        return adapter.query(obd.commands.SPEED, force=True).value.to("mph").magnitude
    except Exception as e:
        log(f"Error occurred while fetching speed: {e}")
        return None


def get_rpm():
    try:
        return adapter.query(obd.commands.RPM, force=True).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching RPM: {e}")
        return None


def get_maf():
    try:
        return adapter.query(obd.commands.MAF, force=True).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching MAF: {e}")
        return None


def get_equiv_ratio():
    try:
        return adapter.query(
            obd.commands.COMMANDED_EQUIV_RATIO, force=True
        ).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching equiv ratio: {e}")
        return None


def get_fuel_level():
    try:
        return adapter.query(obd.commands.FUEL_LEVEL, force=True).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching fuel level: {e}")
        return None


def get_coolant_temp():
    try:
        return adapter.query(obd.commands.COOLANT_TEMP, force=True).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching coolant temp: {e}")
        return None


def get_runtime():
    try:
        return adapter.query(obd.commands.RUN_TIME, force=True).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching runtime: {e}")
        return None


def get_throttle_pos():
    try:
        return adapter.query(
            obd.commands.RELATIVE_THROTTLE_POS, force=True
        ).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching relative throttle position: {e}")
        return None


def get_accel_pos():
    try:
        return adapter.query(obd.commands.ACCELERATOR_POS_D, force=True).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching accelerator position: {e}")
        return None


def get_voltage():
    try:
        return adapter.query(
            obd.commands.CONTROL_MODULE_VOLTAGE, force=True
        ).value.magnitude
    except Exception as e:
        log(f"Error occurred while fetching voltage: {e}")
        return None


def main():
    global adapter
    # Initialize LCD and attempt to connect to OBD adapter, if not detected, keep trying
    lcd_msg("Initializing...", clear_lcd=True, setup=True)
    adapter = obd.OBD()
    last_status = None
    while True:
        status = adapter.status()
        if status is OBDStatus.CAR_CONNECTED:
            lcd_msg("Connected!", clear_lcd=True, setup=True)
            break
        if last_status is not status:
            match status:
                case OBDStatus.NOT_CONNECTED:
                    lcd_msg("Adapter not", "detected...", clear_lcd=True, setup=True)
                case OBDStatus.ELM_CONNECTED:
                    lcd_msg(
                        "Adapter detected",
                        "No car connected",
                        clear_lcd=True,
                        setup=True,
                    )
                case OBDStatus.OBD_CONNECTED:
                    lcd_msg(
                        "Car connected", "Is ignition off?", clear_lcd=True, setup=True
                    )
        last_status = status
        adapter = obd.OBD()
        time.sleep(0.5)

    # If initial loop is exited we must be good to go, dump if required, and open new file for writing
    log("We're ready, go go go...")

    trip_path = setup(adapter)

    # TODO: Change this to handle adapter detachments after the first loop?
    # If the adapter is unplugged mid-loop the script crashes and systemd handles a restart...
    # like, this works?? but definitely not the best way to do this.
    # I believe there's some tolerance now, as invalid query results just return None, have yet to test

    obd_thread = threading.Thread(target=obd_worker, daemon=True)
    obd_thread.start()
    mpg_thread = threading.Thread(target=mpg_worker, daemon=True)
    mpg_thread.start()
    gear_thread = threading.Thread(target=gear_worker, daemon=True)
    gear_thread.start()

    loop_count = 0
    while True:
        loop_count += 1

        # Instant MPG
        lcd_msg("Instant MPG:", clear_lcd=True)
        for _ in range(5):
            impg = state["impg"]
            lcd_msg(None, str(round(impg), 2) if impg is not None else "----")
            time.sleep(1)

        # Average MPG (calculated through mpg_worker())
        lcd_msg("Average MPG:", clear_lcd=True)
        for _ in range(5):
            ampg = state["ampg"]
            lcd_msg(None, str(round(ampg), 2) if ampg is not None else "----")
            time.sleep(1)

        # Coolant temp
        lcd_msg("Coolant temp:", clear_lcd=True)
        for _ in range(5):
            temp = state["coolant_temp"]
            lcd_msg(None, str(temp) + "°C" if temp is not None else "----")
            time.sleep(1)

        # Fuel level
        # TODO: In Oakley's car, this value was jumping around like crazy. Cluster showed around 45%, while the display read anywhere from 60% - 45%.
        # Is this reading accurate while in motion? Probably doesn't account for slosh. An average of the last few readings would likely be
        # better, or perhaps we only read the fuel level when the car is travelling slow enough.
        # Right now, it'll only display the fuel level when we're going slow enough, since any other time, it's probably unreliable.
        fuel_level = state["fuel_level"]
        speed = state["speed"]
        if speed is not None and speed < 3 and fuel_level is not None:
            lcd_msg("Fuel level:", str(round(fuel_level, 1)) + "%", True)
            time.sleep(5)

        # Car trip stats, write aMPG and fuel levels to file.
        # Since we can't safely handle shutdowns, we just write to the file every fifth loop and hope we don't lose power mid-write.
        # This suuuuucks.
        if loop_count % 5 == 0:
            runtime = state["runtime"] if state["runtime"] is not None else 0.0
            ampg = state["ampg"] if state["ampg"] is not None else 0.0
            fuel_level = state["fuel_level"] if state["fuel_level"] is not None else 0.0
            with open(trip_path, "a") as f:
                f.write(
                    str(round(runtime, 2))
                    + ","
                    + str(round(ampg, 2))
                    + ","
                    + str(round(fuel_level, 1))
                    + "\n"
                )
                f.flush()


if __name__ == "__main__":
    main()

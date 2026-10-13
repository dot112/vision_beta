# Real-life tests: triggered industrial cameras

**Read this first.** The driver for GigE Vision and USB3 Vision cameras was written on 2026-10-13 without a camera. Its logic is tested against a stand-in camera (93 automated tests and a run in a live server), but **no line of it has talked to a real camera**. The calls into the camera library were written from the library's documentation and source.

So the first real camera is a test, not an installation. Work through the steps in order. Each step says what to do, what you should see, and what to send back if you do not see it. Stop at the first step that fails: later steps build on it.

Do this on a desk, not on a running line.

## What you need

| | |
|---|---|
| A camera | Any GigE Vision or USB3 Vision camera (Basler, Hikrobot, Daheng, FLIR, IDS, Baumer, Allied Vision, The Imaging Source and others). |
| Its maker's SDK | Free from the maker's website. It installs the GenTL producer file (`.cti`) and sets `GENICAM_GENTL64_PATH`. |
| A network port | For GigE: a port on the same network range as the camera. The maker's tool can set the camera's address. |
| A trigger source | A push button with a power supply, or a PLC output, wired to the camera's input. Check the input's voltage range in the camera's manual first. |
| A model | Any vision model already uploaded to the server. |

The server must run **natively** on Windows for these cameras (`start_server.bat`), or in Docker on Linux with host networking. Docker Desktop on Windows cannot reach them.

## Step 0: install

1. Install the maker's SDK. Open the maker's own viewer and make sure it shows a live picture. **Close the viewer afterwards**: a camera can be open in one program only.
2. In the server's Python environment: `pip install -r requirements-industrial.txt`.
3. Check the producer file is found. In a new terminal: `echo %GENICAM_GENTL64_PATH%` (Linux: `echo $GENICAM_GENTL64_PATH`). It should name a folder that holds a `.cti` file. If it is empty, set `GENTL_CTI_PATH` in `.env` to that folder or file.

**If the install fails:** send the full text of the error and the output of `python --version`.

## Step 1: does the library see the camera?

With the server stopped:

```
python "test codes/industrial_camera_check.py"
```

**You should see** the producer file's path and one line per camera with its serial number, maker and model.

| You see | What it means |
|---|---|
| `NOT READY: ... requirements-industrial.txt` | Step 0.2 was not done in this Python environment. |
| `NOT READY: No GenTL producer file` | Step 0.3: the `.cti` file is not found. |
| `Cameras found: none` | The camera is off, on another network range, behind a firewall (GigE discovery uses UDP port 3956), or open in another program. |
| The same camera twice, or a camera that will not open in step 2 | Two makers' producers both see it. Set `GENTL_CTI_PATH` to the camera's own maker's `.cti` file only. |

## Step 2: does it open and give pictures?

```
python "test codes/industrial_camera_check.py" SERIAL
```

**You should see** `Connected`, a list of features, `Trigger inputs it offers` (write these names down: you need one in step 4), `Settings it refused: none`, `Link check: answered`, and `Free running: N picture(s)` (3 or more) with the picture's size.

| You see | What to do |
|---|---|
| `COULD NOT CONNECT` | Send the whole output. Common causes: the camera is open elsewhere, or its address is in another range. |
| `NO PICTURE` | Send the whole output. Try once more after loading the camera's default settings in the maker's viewer (User Set: Default). |
| `picture error: Pixel format ... is not supported` | Send the output. In the maker's viewer set the pixel format to Mono8, an 8-bit Bayer format, RGB8 or BGR8. |
| `Link check FAILED` | Send the output. The camera will still work; an unplugged cable will be noticed late (step 7). |

**This is the most important step.** Whatever it prints, save it: it tells us the names this camera uses for its features.

## Step 3: software trigger

```
python "test codes/industrial_camera_check.py" SERIAL software
```

**You should see** `Trigger the camera accepted: software` and three lines `trigger N: picture W x H after T ms`. Write down the times: that is how late a software-triggered picture is, before the model runs.

If it says `accepted: off`, the camera refused the trigger settings; `Settings it refused` says which. Send the output.

## Step 4: hardware trigger

Wire the button or PLC output to the camera's input. Use one of the input names from step 2 (often `Line0` or `Line1`):

```
python "test codes/industrial_camera_check.py" SERIAL hardware Line1 30
```

Press the button 10 times within the 30 seconds.

**You should see** exactly 10 lines `picture N`, and `Lost in a burst: 0`.

| You see | What it means |
|---|---|
| No pictures | The wiring, the voltage, or the wrong input name. Check the input's state in the maker's viewer while pressing. |
| More pictures than presses | The button bounces. Use a PLC output or a sensor, or set a debounce time in the maker's viewer (Line Debouncer). |
| Pictures without pressing | The trigger was not accepted (see `Trigger the camera accepted`). Send the output. |

## Step 5: in the server

Start the server. On **Line setup**:

1. **Add camera** → **Scan industrial cameras**. The camera is listed with its model and serial number. **Add to the camera list**, then **Add camera**.
2. On its card open **Image**. Set **Trigger** to *Hardware*, **Trigger input** to the name from step 4. Under **Counting** set **Count mode** to *One picture per trigger* and **When nothing is found** to *Good*. Choose a model. **Apply**, then start the line.
3. The card's Image part should say `trigger now: Hardware · waiting for a trigger`.
4. Press the button 10 times.

**You should see** 10 products on the Line dashboard, each with its picture, and the card saying `last picture at <time> (10 so far)`.

Then set **Trigger** to *Software*, **Apply**, and on the **PLC signals** step add a signal with the action *Take a picture* for this camera (the PLC simulator works: `python "test codes/plc_simulator.py" --port 15020`). Each time the signal comes on: one product.

**If a number is wrong:** send the server's log (`logs/`) for those minutes and a screenshot of the camera card with Image open.

## Step 6: a burst

Trigger faster than the model runs: 20 pulses within one second (a PLC output, not a hand).

**You should see** 20 products, in order, a little later. No alarm. Up to 64 pictures wait their turn.

Then a burst the server cannot keep up with: 64 pictures can wait, so send clearly more than 64 plus what the model gets through in that time (for a model that takes 100 ms a picture: 150 pulses within two seconds). **You should see** the alarm *Camera pictures arrive faster than they are inspected*, fewer products than pulses, and the card saying how many pictures were lost. The alarm clears a minute after the last lost picture.

## Step 7: idle and unplugged

1. Leave the camera 30 minutes with no trigger. **You should see** it still connected, with no alarm.
2. Pull its cable. **You should see**, within about 15 seconds, the alarm *Camera stalled* saying the camera is not answering.
3. Plug it in again. **You should see** the alarm clear within about a minute, without touching anything (the server tries again after 2, 4, 8, 16 and then every 30 seconds), and the next trigger makes a product. The trigger settings were written to the camera again.

**If step 2 shows no alarm:** the link check does not notice a lost camera on this maker's driver. Send the output of step 2 above. Until it is fixed, a pulled cable on a triggered camera shows only as "no products".

## Step 8: two cameras

With the first camera connected and its line running, plug in a second one and press **Scan industrial cameras**.

**You should see** the second camera in the list, and the first camera **keeps running** (trigger it during the scan to check).

**If the first camera stops or reconnects during the scan:** send the server's log. Until it is fixed: scan and add all cameras before starting the lines.

**If the second camera is not found** while the first is connected, but is found after disconnecting the first: that is a known limit for a camera on a network port that was not in use at the first scan.

## Step 9: the code reader

A camera with the job *Code reader* and **Read codes** on *When a PLC signal asks*:

- **Trigger: Hardware**: every picture the button takes is read once. Put the same code in front of it twice: two reads.
- **Trigger: Software** with a *Take a picture* signal card: each signal takes one picture and reads it. Cover the code: it takes up to 3 pictures before giving "no code".

## Step 10: the strobe output

Set **Strobe output** to one of the camera's output lines and put a meter or a light on it. **You should see** the line on only while a picture is taken. If the card lists a refused setting for it, this camera names that feature differently: send the output of step 2.

## What to send back when a step fails

1. The whole output of `python "test codes/industrial_camera_check.py" SERIAL` (step 2), even if that step passed.
2. The camera's maker and model, and the SDK's version.
3. The server's log for those minutes, if the server was running.
4. Which step, what you did, what you saw.

## What is known not to be built

- Level triggers (high level, low level), pulse-width (bulb) exposure, the trigger divider.
- CoaXPress cameras and PTP (IEEE 1588) timed triggers.
- The Line dashboard's camera tile shows the last picture as a still. Before the first trigger it is empty; the camera's card on Line setup says "waiting for a trigger".
- Clearing **Strobe output** does not switch the camera's line back; power the camera off and on, or set it in the maker's viewer.
- A camera on Software trigger that both counts (*One picture per trigger*) and reads codes (*When a PLC signal asks*) takes two pictures per signal, one for each.

## Trying it without a camera

Set `GENTL_CTI_PATH=simulator` in `.env` and start the server. **Scan industrial cameras** then finds two stand-in cameras. On Hardware trigger a stand-in takes a picture every 2 seconds; on Software trigger one per trigger. Steps 5, 6 (not the burst) and 9 can be walked through this way to learn the screens. It proves nothing about a real camera.

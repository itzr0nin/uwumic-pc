# UwU Mic - PC app (Windows)

Receives the phone's microphone and plays it into an audio device. Pair it with the UwU Mic phone app.

## One-time setup: virtual microphone
Install the free VB-Cable driver (vb-audio.com/Cable), then reboot.
It creates "CABLE Input" (where this app plays) and "CABLE Output" (the microphone your apps pick).

## Use
1. Phone: open UwU Mic and tap Start. Note the IP and port it shows.
2. PC: open UwUMicClient.exe, type the phone's IP and port, choose "CABLE Input" as the output device.
3. Press Connect. In Discord/OBS/games choose "CABLE Output" as the microphone.
**Play on speaker (test):** off by default. Tick it to also hear the received voice on a speaker or headphones
(pick which one in the list under the box) while the other apps still get the voice through CABLE. Untick to stop.
Tip: use headphones, or the phone mic will pick up the speaker and echo.
By default nothing plays on your speakers: the app only auto-selects "CABLE Input", never your speakers, and it warns you
(orange hint + a question when you press Connect) if the Output device is a normal speaker or headphone device.
Still hearing yourself? Windows may be monitoring the cable: Sound settings > Recording > "CABLE Output" > Properties >
Listen tab > untick "Listen to this device".

One button connects and disconnects. The bar is the loudness meter and the volume slider in one: the bright fill is how loud the
phone is, the hexagon handle is the volume (0-100%). Mute moves the handle to 0 and empties the meter. A global changeable hotkey
(click the hotkey button, press your keys) toggles mute.
If Windows SmartScreen warns about an unknown publisher: More info > Run anyway.

## How to build the EXE

### Option A - GitHub (nothing to install)
1. Create a free account at github.com and a new repository (e.g. "uwumic-pc").
2. Upload everything in this folder, including the `.github` folder, and Commit.
3. Open the **Actions** tab > **Build Windows app** > **Run workflow** (it also runs on every upload).
4. When it turns green, open the run and download the **UwUMicClient-exe** artifact. Unzip it to get UwUMicClient.exe.

### Option B - On your PC
1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
2. Double-click `build_exe.bat`. When it finishes, your program is `dist\UwUMicClient.exe`.

### Run without building an EXE
Double-click `run.bat` (installs the requirements and starts the app).

## Protocol (for reference)
TCP. Server sends "UWUM" + sampleRate (int32 big-endian) + channels (1) + bits (16),
then raw little-endian PCM16 mono.

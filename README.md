# UwU Mic - PC app (Windows)

Receives the phone's microphone and plays it into an audio device. Pair it with the UwU Mic phone app.

## Virtual microphone (automatic)
Apps like Discord and games need a "microphone" device. Windows has no built-in way to make one, so UwU Mic uses the free
VB-Cable driver and sets it up for you the first time you open the app (and again if you press Connect while it is missing):
1. downloads VB-Cable from the official site (vb-audio.com),
2. checks the installer's digital signature (it only continues if it is valid and from VB-Audio),
3. starts the installer with administrator permission (Windows shows its own permission prompt),
4. notices the new device and uses it automatically.
UwU Mic itself shows no questions or buttons for this. Only Windows' own prompts appear: the administrator permission (UAC) and
the driver confirmation (tick "Always trust software from VB-Audio Software" so it never asks again). If CABLE does not show up
afterwards, restart Windows once. The app tries the automatic setup on up to 3 launches; pressing Connect always tries again.
If something goes wrong, the reason is shown at the bottom of the window and every step is written to
`%APPDATA%\\UwUMicClient.log` (Win+R, paste that path). Send that file when asking for help.
VB-Cable is donationware by VB-Audio: please consider supporting them (vb-audio.com/Cable).

**Optional - bundle the driver into your own build (no download):** download the official VB-Cable zip yourself
(vb-audio.com/Cable), save it next to uwumic_client.py as `vbcable.zip`, then build (GitHub Actions and build_exe.bat both pick it
up automatically). Keep that repo private and do not hand the exe to other people unless VB-Audio agrees to it.
The driver itself is not bundled by default because it must be signed by its author and its licence requires an agreement
for redistribution.

## Noise cancellation (RNNoise)
The **Noise cancellation** checkbox (on by default) runs the received voice through RNNoise, a small neural network made to
remove background noise (fans, hiss, room noise, a lot of keyboard clatter) from speech. It runs on the PC, so every phone behaves
the same and the phone stays cool. Tick **Play on speaker (test)** and switch noise cancellation on and off to hear the difference.
It is tuned for voices: it can make music or whistling sound odd, and it will not remove every click while you are talking.
If it cannot start (for example on Windows on ARM), the box unticks itself, the status says why, and your voice keeps working unprocessed.
The GitHub build also tests it inside the finished exe (`UwUMicClient.exe --selftest result.txt`): look at the "Self-test noise
cancellation" step of the build, or download the `selftest-result` file. "RESULT: OK" means it really works in that exe.

## Use
1. Phone: open UwU Mic and tap Start. Note the IP and port it shows.
2. PC: open UwUMicClient.exe, type the phone's IP and port, press Connect. There is nothing else to choose.
3. Press Connect. In Discord/OBS/games choose "CABLE Output" as the microphone.
**Everything is automatic:** the app finds the virtual microphone and picks the right audio device by itself (no menus).
In Discord, OBS or a game, choose "CABLE Output" as the microphone.
**Play on speaker (test):** off by default. Tick it to also hear the received voice on your Windows default speakers or
headphones, to check that sound is arriving. Untick to stop. Tip: use headphones, or the phone mic will pick up the speaker and echo.
If you still hear yourself without ticking it, Windows may be monitoring the cable: Sound settings > Recording >
"CABLE Output" > Properties > Listen tab > untick "Listen to this device".

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

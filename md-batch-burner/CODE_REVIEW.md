# MiniDisc Batch Burner — Code Review & Fix Plan

**To:** Developer assigned to md-batch-burner  
**From:** Code Review  
**Re:** Getting the app to actually burn MiniDiscs

---

Hey,

I've done a full review of the md-batch-burner codebase. The good news is the data models are clean, the GUI layout is solid, and `audio_converter.py` is well-structured. The bad news is the app can't actually burn anything right now. There are several show-stopping bugs plus a security issue. I've sorted them by severity so you can work through them in order.

---

## 🔴 SHOW-STOPPER #1: The Node.js Helper Is Completely Fake

**File:** `netmd-batch-helper.js`

The helper script simulates everything. `deviceConnected` starts `false`, flips to `true` after 2 seconds via `setTimeout`, and `upload_track` always returns `{ success: true }` without doing anything. The `netmd-js` package in `package.json` is never imported.

**What to do:** This is the biggest piece of work. You need to actually integrate `netmd-js`. Here's a starting point:

```js
// netmd-batch-helper.js — Replace the simulated state with real device calls

const { openPairedDevice, upload, Encoding } = require("netmd-js");
const {
  makeGetAsyncPacketIteratorOnWorkerThread,
} = require("netmd-js/dist/node-encrypt-worker");

let device = null;

async function connectDevice() {
  try {
    device = await openPairedDevice();
    if (device) {
      const name = device.getDeviceName
        ? await device.getDeviceName()
        : "NetMD Device";
      return { connected: true, name };
    }
    return { connected: false, name: null };
  } catch (e) {
    return { connected: false, name: null, error: e.message };
  }
}

async function uploadTrack(trackPath, mode) {
  if (!device) {
    return { success: false, error: "No device connected" };
  }

  // Map mode string to netmd-js Encoding enum
  const encodingMap = {
    SP: Encoding.sp,
    LP2: Encoding.lp2,
    LP4: Encoding.lp4,
  };
  const encoding = encodingMap[mode] || Encoding.sp;

  try {
    // The actual upload call — you'll need to read the file into a buffer
    // and pass it to the upload function. Refer to netmd-js docs for the
    // exact API, as it varies by version.
    const fs = require("fs");
    const data = fs.readFileSync(trackPath);

    await upload(
      device,
      {
        data,
        encoding,
        title: require("path").basename(
          trackPath,
          require("path").extname(trackPath),
        ),
      },
      makeGetAsyncPacketIteratorOnWorkerThread,
    );

    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

async function handleCommand(command) {
  switch (command.action) {
    case "get_device":
      return await connectDevice();
    case "upload_track":
      return await uploadTrack(command.path, command.mode);
    case "eject":
      // netmd-js may not support eject directly —
      // check the library API
      return { success: true };
    default:
      return { error: `Unknown action: ${command.action}` };
  }
}
```

**Important:** The `netmd-js` API has changed across versions. Run `npm install` first, then check what's actually exported from the installed version. The upload flow in particular varies. Look at the `netmd-js` README and examples for the version you're using (^3.2.0).

Also delete the `setTimeout` that simulates device connection — that's the fake part.

---

## 🔴 SHOW-STOPPER #2: Audio Conversion Is Never Called

**File:** `md_batch_burner.py`, inside `BurnWorker.run()`

The burn loop pretends to convert audio but actually just sleeps:

```python
# Current code (lines ~270-275) — does nothing
self.status_updated.emit(f"Converting '{track.name}' to {disc.config.mode} format...")
self.msleep(500)  # Simulate conversion
```

The `audio_converter` module is never imported or used. The raw original file path gets sent straight to the Node.js helper.

**Fix:** Import `audio_converter` at the top of `md_batch_burner.py` and actually call it:

```python
# Add this import at the top of md_batch_burner.py
from audio_converter import convert_for_sp, convert_for_lp2, convert_for_lp4

# Then replace the simulated conversion in BurnWorker.run() with:
import tempfile

# Convert audio
self.status_updated.emit(f"Converting '{track.name}' to {disc.config.mode} format...")

# Create a temp file for the converted output
temp_dir = tempfile.mkdtemp(prefix='md_burn_')
converted_path = os.path.join(temp_dir, Path(track.path).stem + '.raw')

if disc.config.mode == "SP":
    success = convert_for_sp(track.path, converted_path)
elif disc.config.mode == "LP2":
    success = convert_for_lp2(track.path, converted_path)
elif disc.config.mode == "LP4":
    success = convert_for_lp4(track.path, converted_path)
else:
    success = False

if not success:
    self.error_occurred.emit(f"Failed to convert '{track.name}'")
    disc.status = DiscStatus.ERROR
    return

# Upload the CONVERTED file, not the original
self.status_updated.emit(f"Uploading '{track.name}' to MiniDisc...")
try:
    call_helper({
        "action": "upload_track",
        "path": converted_path,  # <-- converted file, not track.path
        "mode": disc.config.mode
    })
finally:
    # Clean up temp files
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
```

---

## 🔴 SHOW-STOPPER #3: Multi-Disc Burning Is Broken — Only Disc 1 Ever Burns

**File:** `md_batch_burner.py`, `BurnWorker.run()`

After the first disc completes, the loop hits a `break` and the thread exits:

```python
# Current code (lines ~278-282)
if disc_idx < len(self.discs) - 1:
    self.status_updated.emit("Disc complete! Waiting for disc swap...")
    disc.status = DiscStatus.WAITING_FOR_DISC
    break  # <-- KILLS THE LOOP. Thread exits. Discs 2+ never burn.
```

Meanwhile, `_on_disc_complete()` in the main window calls `self.burn_worker.pause()` and shows a dialog, then calls `self.burn_worker.resume()`. But `pause()`/`resume()` only toggle `_paused` — the thread has already exited because of the `break`.

**Fix:** Replace the `break` with a pause-and-wait loop so the thread stays alive:

```python
# Replace the break with a blocking wait inside the worker thread:

# After disc complete, wait for user to swap disc
if disc_idx < len(self.discs) - 1:
    disc.status = DiscStatus.WAITING_FOR_DISC
    self.status_updated.emit("Disc complete! Please swap disc...")
    self._paused = True  # Signal that we're waiting

    # Block here until resumed or stopped
    while self._paused:
        if not self._running:
            return
        self.msleep(200)

    self.status_updated.emit(f"Continuing with Disc {disc_idx + 2}...")
```

Then in `_on_disc_complete()` on the main window side, remove the `self.burn_worker.pause()` call (since the worker already paused itself) and just show the dialog:

```python
def _on_disc_complete(self, disc_idx: int):
    """Handle disc completion."""
    if disc_idx < len(self.discs):
        widget = self.discs_layout.itemAt(disc_idx).widget()
        if widget:
            widget.update_status(DiscStatus.COMPLETE)

    self.notifier.notify_disc_complete(disc_idx + 1, len(self.discs))

    if disc_idx < len(self.discs) - 1:
        # Don't call pause() — worker already paused itself
        dialog = DiscSwapDialog(disc_idx + 1, self)
        if dialog.exec() == QDialog.Accepted:
            self.burn_worker.resume()  # This unsets _paused, unblocking the loop
        else:
            self.burn_worker.stop()    # User cancelled — stop everything
```

---

## 🔴 SHOW-STOPPER #4: Device Is Always "Not Connected"

**File:** `md_batch_burner.py`, `call_helper()`

Every call to `call_helper()` spawns a **brand new** `node` process. The helper's `setTimeout` in `netmd-batch-helper.js` sets `deviceConnected = true` after 2 seconds — but that only affects that one process instance, which has already returned its response and exited by then.

Result: `get_device` **always** returns `{ connected: false }`.

**Fix:** You have two options:

**Option A (Recommended): Keep the helper as a long-lived process.**

Instead of spawning a new process per call, start the helper once and communicate over stdin/stdout:

```python
class HelperProcess:
    """Manages a persistent Node.js helper process."""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def start(self):
        script_dir = Path(__file__).parent
        helper_path = script_dir / "netmd-batch-helper.js"
        self._proc = subprocess.Popen(
            ['node', str(helper_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(script_dir)
        )

    def call(self, command_dict: dict) -> dict:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                self.start()
            self._proc.stdin.write(json.dumps(command_dict) + '\n')
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            return json.loads(line)

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc = None
```

This means you'd also need to update the helper JS to handle multiple commands (it already reads line-by-line, so the JS side is fine — just remove the `setTimeout` device simulation and replace it with real `netmd-js` device detection).

**Option B (Simpler but less ideal): Remove the device check from the burn flow**

Don't require `get_device` to return connected before burning. Instead, have the upload itself detect whether a device is present. This is less ideal because the user won't see connection status in the GUI.

---

## 🟡 BUG #5: Device Polling Blocks the GUI Thread

**File:** `md_batch_burner.py`, `_check_device()` and `_start_device_polling()`

`_check_device()` calls `call_helper()` synchronously on the main/GUI thread via a `QTimer`. `call_helper()` spawns a subprocess and waits for it. If Node.js is slow to start, the entire GUI freezes.

**Fix:** Move device polling to a background thread or use `QThread`:

```python
class DeviceChecker(QThread):
    """Background thread for device status checks."""
    device_status = Signal(bool, str)  # connected, device_name

    def __init__(self, helper):
        super().__init__()
        self.helper = helper
        self._running = True

    def run(self):
        while self._running:
            try:
                result = self.helper.call({"action": "get_device"})
                connected = result.get("connected", False)
                name = result.get("name", "Unknown")
                self.device_status.emit(connected, name)
            except Exception:
                self.device_status.emit(False, "")
            self.msleep(5000)

    def stop(self):
        self._running = False
        self.wait()
```

Then connect `device_status` signal to a slot that updates the label.

---

## 🟡 BUG #6: Disc Status Contradiction (COMPLETE then WAITING_FOR_DISC)

**File:** `md_batch_burner.py`, `BurnWorker.run()`

```python
disc.status = DiscStatus.COMPLETE           # Set to COMPLETE
self.disc_complete.emit(disc_idx)           # Signal fires
# ...
disc.status = DiscStatus.WAITING_FOR_DISC   # Immediately overwritten
```

The signal handler updates the UI to "COMPLETE", then the model is silently changed to "WAITING_FOR_DISC" with no UI update.

**Fix:** Remove the COMPLETE assignment before the emit, or structure it so WAITING_FOR_DISC is set first, and COMPLETE is only set when truly done:

```python
# Disc finished burning all tracks
self.disc_complete.emit(disc_idx)

if disc_idx < len(self.discs) - 1:
    # More discs to go — signal that we need a swap
    disc.status = DiscStatus.WAITING_FOR_DISC
    self._paused = True
    while self._paused:
        if not self._running:
            return
        self.msleep(200)
else:
    # Last disc — mark complete
    disc.status = DiscStatus.COMPLETE
```

And have `_on_disc_complete()` set the widget to COMPLETE only after confirming it's the last disc.

---

## 🟡 BUG #7: `header_layout` Is Never Added to the Widget

**File:** `md_batch_burner.py`, `DiscWidget.__init__()`

```python
layout = QVBoxLayout(self)

header_layout = QHBoxLayout()                 # Created...
self.status_label = QLabel()
header_layout.addWidget(self.status_label)    # Label added to it...

# But header_layout is NEVER added to `layout`!
# It just floats in memory. The status label is invisible.

self.progress_bar = QProgressBar()
layout.addWidget(self.progress_bar)           # This goes directly into the VBox
```

**Fix:** Add the header layout to the main layout:

```python
header_layout = QHBoxLayout()

self.status_label = QLabel()
self.status_label.setAlignment(Qt.AlignRight)
self.status_label.setStyleSheet("font-weight: bold;")
header_layout.addWidget(self.status_label)

layout.addLayout(header_layout)  # <-- ADD THIS LINE

# Progress bar
self.progress_bar = QProgressBar()
```

---

## 🟡 BUG #8: `dd` Doesn't Exist on Windows

**File:** `audio_converter.py`, `_encode_atrac3_psp()` and `_strip_oma_header()`

Both functions shell out to Unix `dd` to strip file headers. This will crash on Windows with `FileNotFoundError`.

**Fix:** Replace `dd` with pure Python — it's just skipping bytes:

```python
def _strip_header(input_path: str, output_path: str, header_size: int = 96) -> bool:
    """Strip header bytes from a file (cross-platform replacement for dd)."""
    try:
        with open(input_path, 'rb') as f_in:
            f_in.seek(header_size)
            data = f_in.read()
        with open(output_path, 'wb') as f_out:
            f_out.write(data)
        return True
    except Exception as e:
        logger.error(f"Header stripping failed: {e}")
        return False
```

Then replace both `dd` subprocess calls with `_strip_header(oma_path, output_path)`.

---

## 🔴 SECURITY: Command Injection in Toast Notifications

**File:** `md_batch_burner.py`, `Notifier._show_toast()`

The `title` and `message` variables are interpolated directly into:

- An AppleScript string (macOS): `f'display notification "{message}" with title "{title}"'`
- A PowerShell XML template (Windows): `<text id="1">{title}</text>`

If a track is named something like `Track"; do shell script "rm -rf /"` (macOS) or `<img src=x onerror=...>` (Windows XML), it could break or execute arbitrary commands. This is especially dangerous on the PowerShell path since the XML is passed to `powershell -Command`.

**Fix:** Sanitize the inputs. For the PowerShell/XML path, escape XML entities. For osascript, escape quotes:

```python
import html  # at the top of the file

def _show_toast(self, title: str, message: str):
    """Show desktop toast notification."""
    try:
        if sys.platform == "linux":
            # notify-send handles escaping itself since title/message are separate args
            subprocess.run(
                ["notify-send", "-u", "normal", "-t", "5000",
                 "-i", "media-optical", title, message],
                capture_output=True, timeout=5
            )
        elif sys.platform == "darwin":
            # Escape backslashes and quotes for AppleScript
            safe_title = title.replace('\\', '\\\\').replace('"', '\\"')
            safe_message = message.replace('\\', '\\\\').replace('"', '\\"')
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5
            )
        elif sys.platform == "win32":
            # Escape XML special characters
            safe_title = html.escape(title)
            safe_message = html.escape(message)
            # ... rest of PowerShell template using safe_title and safe_message
    except Exception:
        pass
```

---

## 🟡 BUG #9: Test File Catches Wrong Exception Name

**File:** `test_helper.py`, line ~107

```python
except AssertionError as e:  # TYPO: should be AssertionError
```

`AssertionError` doesn't exist, so `assert` failures (which raise `AssertionError`) will fall to the `except Exception` handler and print "Unexpected error" instead of "Test failed."

**Fix:**

```python
except AssertionError as e:
```

---

## 🟡 BUG #10: `capacity_seconds` Returns Float, Annotated as `int`

**File:** `md_batch_burner.py`

```python
@property
def capacity_seconds(self) -> int:
    mode_factors = {"SP": 1.0, "LP2": 2.0, "LP4": 4.0}
    return self.disc_size * 60 * mode_factors.get(self.mode, 1.0)
    # Returns 8880.0 for LP2, not 8880
```

**Fix:** Either change the return type to `float`, or cast to int:

```python
@property
def capacity_seconds(self) -> int:
    mode_factors = {"SP": 1, "LP2": 2, "LP4": 4}
    return self.disc_size * 60 * mode_factors.get(self.mode, 1)
```

Using integer factors is cleaner since disc capacities are always whole numbers of seconds.

---

## 🟢 MINOR: DropZone Eats All Mouse Clicks

**File:** `md_batch_burner.py`, `DropZone.mousePressEvent()`

Any click (left, right, middle) on the drop zone opens a file dialog. Right-clicks should probably be ignored.

**Fix:**

```python
def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Audio Files",
            "",
            "Audio Files (*.mp3 *.wav *.flac *.ogg *.m4a *.aac);;All Files (*)"
        )
        if files:
            self.files_dropped.emit(files)
    else:
        super().mousePressEvent(event)
```

---

## 🟢 MINOR: Changing Default Config Doesn't Re-split

**File:** `md_batch_burner.py`, `_update_default_config()`

When `auto_split` is off, changing from LP4 to SP updates disc configs but doesn't check if tracks still fit. You could end up with a disc showing 296 minutes of tracks assigned to a 74-minute SP config.

**Fix:** When auto_split is off and the config changes, at minimum show a warning if any disc is over capacity:

```python
def _update_default_config(self):
    self.default_config.disc_size = [60, 74, 80][self.default_size_combo.currentIndex()]
    self.default_config.mode = self.default_mode_combo.currentText()

    if self.auto_split:
        self._split_into_discs()
    else:
        for disc in self.discs:
            disc.config = DiscConfig(self.default_config.disc_size, self.default_config.mode)
        self._refresh_disc_widgets()

        # Warn about over-capacity discs
        over_capacity = [d for d in self.discs if d.total_seconds > d.config.capacity_seconds]
        if over_capacity:
            QMessageBox.warning(
                self,
                "Over Capacity",
                f"{len(over_capacity)} disc(s) exceed capacity with the new settings. "
                "Enable Auto-Split or remove tracks manually."
            )
```

---

## 🟢 MINOR: Stale Path in QUICKSTART.md

**File:** `QUICKSTART.md`

```
cd /home/james/.openclaw/workspace/innovation/projects/md-batch-burner
```

This is your local dev path. Replace it with something generic for end users.

---

## 🟢 MINOR: No .gitignore

`__pycache__/` is being tracked. Add a `.gitignore`:

```
__pycache__/
*.pyc
node_modules/
package-lock.json
.env
```

---

## Recommended Order of Work

Here's how I'd tackle this if I were you:

1. **Fix the test typo** (`AssertionError` → `AssertionError`) and run the tests. They should pass. This gives you confidence the data models work. (5 min)

2. **Fix `header_layout` not being added** in `DiscWidget`. Quick one-liner. (2 min)

3. **Fix `capacity_seconds` type** — use integer mode factors. (2 min)

4. **Replace `dd` calls** with the pure Python `_strip_header()` function. (15 min)

5. **Fix the command injection** in `_show_toast()`. (15 min)

6. **Rewrite `call_helper()` to use a persistent process** (`HelperProcess` class). This unblocks both the "always disconnected" bug and the "GUI freezes on poll" bug. (1-2 hours)

7. **Fix the multi-disc burning loop** — replace `break` with pause-and-wait. This is the trickiest part because you need to coordinate between the worker thread and the main thread correctly. Test with 2+ fake discs first. (2-3 hours)

8. **Wire up `audio_converter`** in the burn loop. Test with a real audio file → SP conversion first, then LP2/LP4. (1-2 hours)

9. **Implement real `netmd-js` calls** in the helper. This is the final piece. You'll want a real MiniDisc device to test with. (4+ hours, heavily dependent on the `netmd-js` API)

10. **Clean up** — fix the DropZone click handling, add `.gitignore`, fix `QUICKSTART.md` path, add the over-capacity warning. (30 min)

Items 1-5 are quick wins you can knock out first. Items 6-8 are the real engineering work. Item 9 requires hardware and is where you'll spend the most time debugging.

Good luck — the bones of this app are solid. It just needs the real plumbing connected.

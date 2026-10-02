import os
import subprocess
import threading

custom_env = os.environ.copy()
custom_env["PYTHONPATH"] = "src"

CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

PREFIX = "session: sessions/"
session_id = None
session_found = threading.Event()


def stream_camera(proc):
    global session_id
    for line in proc.stdout:
        print(f"{CYAN}[CAMERA] {line.rstrip()}{RESET}", flush=True)
        if PREFIX in line and not session_found.is_set():
            session_id = line.split(PREFIX)[1].strip()
            session_found.set()
    session_found.set()  # unblock the wait below even if the session line never showed up (e.g. a crash)


def stream_can(proc):
    for line in proc.stdout:
        print(f"{MAGENTA}[CAN] {line.rstrip()}{RESET}", flush=True)


camera = subprocess.Popen(
    ["python3", "-m", "ivision.frames.record", "--source", "picamera2", "--width", "1280",
     "--height", "1280", "--quality", "90", "--encode-workers", "3", "--out", "sessions",
     "--status-every", "10"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env=custom_env,
)
camera_thread = threading.Thread(target=stream_camera, args=(camera,), daemon=True)
camera_thread.start()

session_found.wait()

can = None
if session_id:
    can = subprocess.Popen(
        ["python3", "-m", "ivision.can.record", "--source", "socketcan", "--interface", "can0",
         "--session", f"sessions/{session_id}", "--status-every", "10"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=custom_env,
    )
    can_thread = threading.Thread(target=stream_can, args=(can,), daemon=True)
    can_thread.start()

camera.wait()
camera_thread.join()
if can is not None:
    can.wait()
    can_thread.join()

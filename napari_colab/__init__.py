"""
napari_colab.py
???????????????
Interactive napari viewer inside Google Colab via noVNC.

Usage
-----
    from napari_colab import setup, open_viewer, screenshot, shutdown

    url = setup()                    # run ONCE per session ? open URL in new tab
    viewer = open_viewer(1800, 1000) # launch napari

    from pathlib import Path
    raw    = Path('BBBC014_v1_images_tiff/')
    labels = Path('BBBC014_label_folder/')

    viewer.open(sorted(raw.glob('*Channel 2*.tiff')), stack=True, name='nuclei')
    viewer.open(sorted(raw.glob('*Channel 1*.tiff')), stack=True, name='nfkappab')
    viewer.open(sorted(labels.glob('*Channel*.tiff')), stack=True,
                layer_type='labels', name='label')

    viewer.dims.ndisplay = 3         # switch to 3-D view
    viewer.camera.angles = (30,45,0)
    viewer.reset_view()

    screenshot()                     # inline preview anytime
    shutdown()                       # clean up at end of session

Architecture
------------
  napari runs in a *subprocess* (so the Colab kernel never crashes).
  Commands are sent via a tiny JSON-over-TCP socket on port 9999.
  A QTimer drains the command queue every 200 ms on the Qt main thread,
  which is the only thread allowed to touch Qt/napari objects.
"""

import os, sys, time, socket, struct, json, base64, subprocess, fcntl, threading, tempfile
from pathlib import Path

# ?? module-level state ????????????????????????????????????????????????????????
proc      = None   # the napari subprocess (kept as module-level so it survives cells)
_setup_done = False
_CMD_PORT = 9999


# ?????????????????????????????????????????????????????????????????????????????
#  PUBLIC API
# ?????????????????????????????????????????????????????????????????????????????

def setup(display=':99', vnc_port=5900, novnc_port=6080,
          resolution='1600x1000x24'):
    """
    Install system deps, start Xvfb + x11vnc + noVNC websockify.
    Call once per Colab session.  Returns the noVNC browser URL.
    """
    global _setup_done
    _install_deps()
    _start_xvfb(display, resolution)
    _start_x11vnc(display, vnc_port)
    _start_novnc(vnc_port, novnc_port)

    _setup_done = True
    return _colab_url(novnc_port)


def open_viewer(width=1800, height=1000):
    """
    Launch napari in a subprocess.  Returns a ViewerProxy.
    Subsequent calls return the same viewer if it is still alive.
    """
    global proc

    if not _setup_done:
        setup()

    if proc is not None and proc.poll() is None:
        print("napari already running ? returning existing proxy.")
        return ViewerProxy()

    script = _build_server_script(width, height)
    fd = tempfile.NamedTemporaryFile(mode='w', suffix='.py', prefix='_napari_server_', delete=False)
    fd.write(script)
    fd.close()
    spath = fd.name

    env  = _build_env()
    proc = subprocess.Popen(
        [sys.executable, spath],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # merge ? one stream to monitor
        env=env,
    )

    print(f"napari PID {proc.pid} ? streaming startup output:\n")
    fcntl.fcntl(proc.stdout, fcntl.F_SETFL, os.O_NONBLOCK)

    ready    = False
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            line = proc.stdout.readline().decode(errors='replace')
            if line:
                print(line, end='', flush=True)
                if 'READY' in line:
                    ready = True
                    break
        except Exception:
            pass
        if proc.poll() is not None:
            print(f"\n? napari subprocess died (code {proc.poll()})")
            try:
                rest = proc.stdout.read().decode(errors='replace')
                if rest: print(rest)
            except Exception: pass
            raise RuntimeError("napari failed to start ? see output above")
        time.sleep(0.05)

    url = _colab_url(6080)
    if ready:
        print(f"\n? napari ready ? open in a NEW browser tab:\n\n   {url}\n")
    else:
        print(f"\n??  No READY signal ? napari may still be starting.\n   {url}\n")

    # background thread: keep draining stdout so the pipe never blocks
    threading.Thread(target=_drain_stdout, args=(proc,), daemon=True).start()

    return ViewerProxy()


def screenshot(path='/tmp/_napari_screen.png'):
    """Capture the virtual display and show it inline in the notebook."""
    r = subprocess.run(
        ['scrot', path],
        env={**os.environ, 'DISPLAY': ':99'},
        capture_output=True,
    )
    if r.returncode != 0:
        print("scrot failed:", r.stderr.decode())
        return
    try:
        from IPython.display import Image, display
        display(Image(path))
    except ImportError:
        print(f"Screenshot saved: {path}")


def shutdown():
    """Terminate napari and all display helpers."""
    global proc
    if proc and proc.poll() is None:
        proc.terminate()
        proc = None
        print("napari stopped.")
    for p in ['Xvfb', 'x11vnc', 'websockify']:
        subprocess.run(['pkill', '-f', p], capture_output=True)
    print("Display helpers stopped.")


# ?????????????????????????????????????????????????????????????????????????????
#  ViewerProxy  ? mirrors the napari.Viewer API, forwards via socket
# ?????????????????????????????????????????????????????????????????????????????

class ViewerProxy:
    """
    Thin proxy for the real napari.Viewer running in the subprocess.

    Supported calls
    ???????????????
    viewer.open(paths, stack=True, name='...', layer_type='image'|'labels')
    viewer.add_image(np_array, name='...', colormap='...', blending='...')
    viewer.dims.ndisplay = 2 | 3
    viewer.camera.angles = (rx, ry, rz)
    viewer.camera.zoom   = float
    viewer.reset_view()
    viewer.window.resize(w, h)
    """

    def open(self, paths, stack=True, name='layer',
             layer_type='image', **kwargs):
        paths = [str(p) for p in paths]
        if not paths:
            print(f"??  No files matched for layer '{name}'")
            return
        print(f"? sending {len(paths)} file(s) as '{name}' [{layer_type}]")
        _send({'action': 'open', 'paths': paths, 'stack': stack,
               'name': name, 'layer_type': layer_type, **kwargs})

    def add_image(self, array, **kwargs):
        import numpy as np
        arr = np.asarray(array)
        payload = {
            'action': 'add_image',
            'array_b64': base64.b64encode(arr.tobytes()).decode('ascii'),
            'array_shape': list(arr.shape),
            'array_dtype': str(arr.dtype),
            **kwargs,
        }
        _send(payload)

    def reset_view(self):
        _send({'action': 'reset_view'})

    @property
    def dims(self):   return _DimsProxy()
    @property
    def camera(self): return _CameraProxy()
    @property
    def window(self): return _WindowProxy()


class _DimsProxy:
    def __setattr__(self, name, value):
        if name == 'ndisplay':
            _send({'action': 'set_ndisplay', 'value': value})
        else:
            super().__setattr__(name, value)

class _CameraProxy:
    def __setattr__(self, name, value):
        _send({'action': 'set_camera', 'attr': name, 'value': value})

class _WindowProxy:
    def resize(self, w, h):
        _send({'action': 'resize_window', 'w': w, 'h': h})


# ?????????????????????????????????????????????????????????????????????????????
#  Command transport (JSON over loopback TCP)
# ?????????????????????????????????????????????????????????????????????????????

def _send(cmd, retries=5):
    for attempt in range(retries):
        try:
            s = socket.create_connection(('localhost', _CMD_PORT), timeout=15)
            data = json.dumps(cmd).encode('utf-8')
            s.sendall(struct.pack('>I', len(data)) + data)
            ack  = s.recv(4)
            s.close()
            if ack == b'ACK!':
                return
        except Exception as e:
            if attempt == retries - 1:
                print(f"??  Command '{cmd.get('action')}' failed: {e}")
            time.sleep(1)


# ?????????????????????????????????????????????????????????????????????????????
#  Subprocess server script
# ?????????????????????????????????????????????????????????????????????????????

def _build_server_script(width, height):
    return f'''\
import os, sys, socket, struct, json, base64, threading, queue, traceback
os.environ["DISPLAY"]                  = ":99"
os.environ["LIBGL_ALWAYS_SOFTWARE"]    = "1"
os.environ["MESA_GL_VERSION_OVERRIDE"] = "3.3"
os.environ["QT_QPA_PLATFORM"]          = "xcb"
os.environ["XDG_RUNTIME_DIR"]          = "/tmp/runtime-root"

print("[server] importing napari...", flush=True)
import napari
print("[server] napari imported OK", flush=True)

_q = queue.Queue()

# ?? command executor (runs on Qt main thread via QTimer) ?????????????????????
def _execute(cmd):
    action = cmd.get("action")
    print(f"[server] executing: {{action}}", flush=True)

    if action == "open":
        paths      = cmd["paths"]
        stack      = cmd.get("stack", True)
        name       = cmd.get("name", "layer")
        layer_type = cmd.get("layer_type", "image")
        extra      = {{k: v for k, v in cmd.items()
                       if k not in ("action","paths","stack","name","layer_type")}}
        print(f"[server] viewer.open() {{len(paths)}} paths ? {{name!r}}", flush=True)
        viewer.open(paths, stack=stack, name=name, layer_type=layer_type, **extra)
        viewer.reset_view()
        print(f"[server] ? {{name!r}} loaded", flush=True)

    elif action == "add_image":
        import numpy as np
        arr = np.frombuffer(
            base64.b64decode(cmd.pop("array_b64")),
            dtype=cmd.pop("array_dtype"),
        ).reshape(cmd.pop("array_shape"))
        cmd.pop("action", None)
        viewer.add_image(arr, **cmd)

    elif action == "set_ndisplay":
        viewer.dims.ndisplay = cmd["value"]

    elif action == "set_camera":
        _ALLOWED_CAMERA_ATTRS = {{"angles", "zoom", "center", "interactive", "perspective"}}
        attr = cmd["attr"]
        if attr not in _ALLOWED_CAMERA_ATTRS:
            print(f"[server] blocked set_camera for disallowed attr: {{attr!r}}", flush=True)
        else:
            setattr(viewer.camera, attr, cmd["value"])

    elif action == "reset_view":
        viewer.reset_view()

    elif action == "resize_window":
        viewer.window.resize(cmd["w"], cmd["h"])

def _drain():
    while not _q.empty():
        c = _q.get_nowait()
        try:
            _execute(c)
        except Exception:
            traceback.print_exc()
            sys.stdout.flush()

# ?? socket server (background thread) ????????????????????????????????????????
def _recv_all(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf

def _server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("localhost", {_CMD_PORT}))
    srv.listen(10)
    print("[server] socket listening on {_CMD_PORT}", flush=True)
    while True:
        try:
            conn, _ = srv.accept()
            n   = struct.unpack(">I", _recv_all(conn, 4))[0]
            cmd = json.loads(_recv_all(conn, n).decode("utf-8"))
            print(f"[server] queued: {{cmd.get('action')}}", flush=True)
            _q.put(cmd)
            conn.sendall(b"ACK!")
            conn.close()
        except Exception as e:
            print(f"[server] socket error: {{e}}", flush=True)

threading.Thread(target=_server, daemon=True).start()

# ?? napari viewer ?????????????????????????????????????????????????????????????
print("[server] creating viewer...", flush=True)
viewer = napari.Viewer()
viewer.window.resize({width}, {height})
print("[server] viewer created OK", flush=True)

from qtpy.QtCore import QTimer
_timer = QTimer()
_timer.timeout.connect(_drain)
_timer.start(200)
print("[server] QTimer started (200 ms)", flush=True)

sys.stdout.write("READY\\n")
sys.stdout.flush()

napari.run()
'''


# ?????????????????????????????????????????????????????????????????????????????
#  Infrastructure helpers
# ?????????????????????????????????????????????????????????????????????????????

def _install_deps():
    print("Installing system packages...")
    subprocess.run([
        'apt-get', 'install', '-qq', '-y',
        'xvfb', 'x11vnc', 'scrot',
        'libxcb-icccm4', 'libxcb-image0', 'libxcb-keysyms1',
        'libxcb-randr0', 'libxcb-render-util0', 'libxcb-xinerama0',
        'libxcb-xkb1', 'libxkbcommon-x11-0', 'libxkbcommon0',
        'libgl1-mesa-glx', 'libgl1-mesa-dri', 'libglib2.0-0',
        'libdbus-1-3', 'libxcb-util1', 'libxcb-cursor0',
    ], capture_output=True)
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '-q',
         'websockify', 'napari[all]', 'tifffile', 'qtpy'],
        capture_output=True,
    )
    # noVNC v1.5.0 — pinned to exact commit to prevent tag-rewrite attacks
    _NOVNC_COMMIT = '67129b671d9393212bec7364d15344c6fa5a8ae9'
    novnc_dir = '/opt/novnc'
    if not os.path.isdir(novnc_dir):
        subprocess.run(
            ['git', 'clone', '-q', 'https://github.com/novnc/noVNC.git', novnc_dir],
            capture_output=True,
        )
        subprocess.run(
            ['git', '-C', novnc_dir, 'checkout', _NOVNC_COMMIT],
            capture_output=True,
        )
    print("Dependencies ready ?")


def _start_xvfb(display, resolution):
    subprocess.run(['pkill', '-f', 'Xvfb'], capture_output=True)
    time.sleep(0.5)
    subprocess.Popen(
        ['Xvfb', display, '-screen', '0', resolution, '-ac', '+extension', 'GLX'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    print("Xvfb ?")


def _start_x11vnc(display, port):
    subprocess.run(['pkill', '-f', 'x11vnc'], capture_output=True)
    time.sleep(0.5)
    subprocess.Popen(
        ['x11vnc', '-display', display, '-nopw', '-forever',
         '-shared', '-rfbport', str(port), '-quiet'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    print("x11vnc ?")


def _start_novnc(vnc_port, novnc_port):
    subprocess.run(['pkill', '-f', 'websockify'], capture_output=True)
    time.sleep(0.5)
    subprocess.Popen(
        [sys.executable, '-m', 'websockify',
         '--web=/opt/novnc/', str(novnc_port), f'localhost:{vnc_port}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    print("noVNC ?")


def _build_env():
    return {
        **os.environ,
        'DISPLAY':                  ':99',
        'LIBGL_ALWAYS_SOFTWARE':    '1',
        'MESA_GL_VERSION_OVERRIDE': '3.3',
        'QT_QPA_PLATFORM':          'xcb',
        'XDG_RUNTIME_DIR':          '/tmp/runtime-root',
    }


def _port_open(port, timeout=2):
    try:
        s = socket.create_connection(('localhost', port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _colab_url(port):
    try:
        from google.colab.output import eval_js
        base = eval_js(f"google.colab.kernel.proxyPort({port})")
        return (f"{base}/vnc.html"
                f"?autoconnect=true&resize=scale&quality=8&compression=3")
    except ImportError:
        return f"http://localhost:{port}/vnc.html?autoconnect=true&resize=scale"


def _drain_stdout(p):
    """Keep reading subprocess stdout so the OS pipe buffer never fills."""
    try:
        for line in p.stdout:
            pass
    except Exception:
        pass
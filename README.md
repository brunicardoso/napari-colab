# napari-colab

```
                         _  ____      _       _
 _ __   __ _ _ __   __ _| |/ ___|___ | | __ _| |__
| '_ \ / _` | '_ \ / _` | | |   / _ \| |/ _` | '_ \
| | | | (_| | |_) | (_| | | |__| (_) | | (_| | |_) |
|_| |_|\__,_| .__/ \__,_|_|\____\___/|_|\__,_|_.__/
             |_|
```

**napari :heart: Colab** — because microscopy images deserve a real viewer, even in the cloud.

Interactive [napari](https://napari.org) viewer inside Google Colab via noVNC.

> **Warning:** This package is designed exclusively for Google Colab. It relies on
> root access via `apt-get`, a virtual framebuffer (Xvfb), and the Colab proxy to
> tunnel ports to your browser. It will not work on local machines, JupyterHub,
> or other hosted notebook services. If you can run napari locally, you should —
> it will be faster and more responsive.

## Installation

```bash
pip install napari-colab
```

> **Note:** the system packages (`xvfb`, `x11vnc`, `scrot`, xcb libraries) are
> installed automatically by `setup()` on first run via `apt-get`.

## Quick start

```python
# Cell 1 — once per session
from napari_colab import setup, open_viewer, screenshot, shutdown

# Cell 2 — launch viewer (URL printed here)
viewer = open_viewer(width=1800, height=1000)

# Cell 3 — load your data
from pathlib import Path
images = sorted(Path('my_images/').glob('*.tiff'))
labels = sorted(Path('my_labels/').glob('*.tiff'))

viewer.open(images, stack=True, name='raw')
viewer.open(labels, stack=True, layer_type='labels', name='segmentation')

# Cell 4 — optional extras
viewer.dims.ndisplay = 3
viewer.camera.angles = (30, 45, 0)
screenshot()   # inline preview
```

## API

| Function | Description |
|---|---|
| `setup()` | Install deps, start Xvfb + x11vnc + noVNC |
| `open_viewer(width, height)` | Launch napari subprocess, return `ViewerProxy` |
| `screenshot()` | Capture virtual display inline in notebook |
| `shutdown()` | Kill napari and display helpers |

### ViewerProxy

```python
viewer.open(paths, stack=True, name='layer', layer_type='image'|'labels')
viewer.add_image(np_array, name='...', colormap='...', blending='...')
viewer.add_labels(np_array, name='...')
viewer.dims.ndisplay = 2 | 3
viewer.camera.angles = (rx, ry, rz)
viewer.camera.zoom   = 1.5
viewer.reset_view()
viewer.window.resize(w, h)
```

## How it works

```
Xvfb (:99)   <- napari + Qt render here (software OpenGL / Mesa)
    |
x11vnc       <- captures framebuffer -> VNC (port 5900)
    |
websockify   <- VNC -> WebSocket bridge (port 6080)
    |
noVNC        <- web client at /vnc.html
    |
Colab proxy  <- tunnels port 6080 to your browser tab
```

napari runs in its own subprocess so the Colab kernel never crashes.
Commands (open, add_image, camera, etc.) are forwarded via a
JSON-over-TCP socket. A `QTimer` drains the command queue every 200 ms
on the Qt main thread — the only thread allowed to touch Qt objects.


## Troubleshooting

### OpenCV Qt conflict (Cellpose and similar packages)

Packages like [Cellpose](https://github.com/MouseLand/cellpose) pull in `opencv-python`, which bundles its own Qt plugins. These conflict with the Qt used by napari, causing a fatal `xcb` plugin error:

```
qt.qpa.plugin: Could not load the Qt platform plugin "xcb"
```

`napari-colab` depends on `opencv-python-headless` (which is functionally identical but Qt-free) to avoid this. However, if you install a package that depends on `opencv-python` **after** `napari-colab`, pip may re-install the full OpenCV and reintroduce the conflict.

**Fix:**

```bash
pip uninstall -y opencv-python && pip install opencv-python-headless
```

**Prevention:** install OpenCV-dependent packages (cellpose, etc.) *before* `napari-colab` so that the headless variant wins:

```bash
pip install cellpose
pip install napari-colab
```

## License

MIT

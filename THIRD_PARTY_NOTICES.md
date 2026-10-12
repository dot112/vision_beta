# Third-party notices

Zagel Vision uses the open-source components below. Each remains under its
own license, which applies to that component instead of the project
[LICENSE](LICENSE). Licenses were listed from each project's published
metadata and general knowledge, **not** from a full automated scan: before a
release, regenerate and confirm them (for example `pip-licenses` in the
production virtual environment) and add the license texts that your
distribution method requires.

## Runtime (`requirements.txt`)

| Component | License |
|---|---|
| FastAPI, Starlette (BSD-3-Clause), Uvicorn | MIT / BSD-3-Clause |
| Pydantic, pydantic-settings | MIT |
| SQLAlchemy, Alembic, aiosqlite | MIT |
| anyio, h11, httpx, httpcore | MIT / BSD-3-Clause |
| python-dotenv, python-multipart, websockets | BSD-3-Clause / Apache-2.0 / BSD-3-Clause |
| python-jose, passlib | MIT / BSD |
| bcrypt, cryptography | Apache-2.0 (cryptography: Apache-2.0 OR BSD-3-Clause) |
| requests, packaging | Apache-2.0 (packaging: Apache-2.0 OR BSD-2-Clause) |
| paho-mqtt | EPL-2.0 / EDL-1.0 (BSD-3-Clause) |
| protobuf | BSD-3-Clause |
| asyncua (OPC UA) | LGPL-3.0 — keep it as a separate, replaceable library; do not modify it without publishing the changes |
| pyserial | BSD-3-Clause |
| OpenCV (opencv-python-headless) | Apache-2.0 (the wheel also bundles FFmpeg under LGPL-2.1) |
| NumPy | BSD-3-Clause |
| Pillow | MIT-CMU (HPND) |
| zxing-cpp | Apache-2.0 |
| ONNX Runtime, ONNX Runtime DirectML | MIT |
| psutil | BSD-3-Clause |
| nvidia-ml-py | BSD-3-Clause |
| PyYAML | MIT |
| greenlet | MIT / PSF |
| tzdata | Apache-2.0 |

## Training only (`requirements-training.txt`)

| Component | License | Note |
|---|---|---|
| **Ultralytics (YOLO)** | **AGPL-3.0** (or the paid Ultralytics Enterprise license) | Strong copyleft. Distributing software that uses it, or offering it as a network service, requires publishing the complete corresponding source under AGPL-3.0 unless you hold the Enterprise license. YOLO model weights carry the same terms. Keep it out of any proprietary distribution that you do not intend to open-source or license from Ultralytics. |
| PyTorch, torchvision | BSD-3-Clause | |

## Models and datasets

Detection models and datasets that you import or train are licensed by their
own authors. Check them for commercial-use limits before deploying.

## Reporting

If you find a missing or incorrect notice, tell the project maintainers so it
can be corrected.

# Running HRC Studio Stem on Kaggle

Kaggle gives you a free GPU (T4 x2 / P100) for 30 hours a week, which is plenty for
song-length separations. Nothing in this repo is macOS-specific: on Linux with CUDA the
engine picks the GPU automatically and skips the MPS workarounds entirely.

There is a ready-to-run notebook next to this file: **[`kaggle_quickstart.ipynb`](kaggle_quickstart.ipynb)**.
Click *Copy & Edit* on Kaggle, set **Accelerator = GPU** and **Internet = On**, then run all cells.

---

## 1 · Get the code into the notebook session (Kaggle dataset)

The repo is private, so a **Kaggle dataset** is the cleanest route — no tokens, and the
notebook finds it automatically under `/kaggle/input`.

```bash
# build the upload package (code only, ~500 KB)
zip -qr dist/hrc-studio-stem-kaggle.zip . \
    -x ".venv/*" ".git/*" "work/*" "models/*" "*/__pycache__/*" "*.pyc" "dist/*" "*.log"
```

Then in Kaggle: **Datasets → New Dataset →** drag `dist/hrc-studio-stem-kaggle.zip` in →
*Create*. In your notebook: **Add Data → Your datasets →** pick it.

| Flavour | Contents | Result |
| --- | --- | --- |
| **Code only** (~0.5 MB) | repo, no `models/` | Internet must be **On**; weights download once (~610 MB) into `models/` |
| **Self-contained** (~680 MB) | repo + `models/` | No network needed after install; the notebook copies the weights from the read-only `/kaggle/input` into `/kaggle/working/models` |

Either way the notebook's first cell:

- finds the repo by looking for `stemkit/separator.py` under `/kaggle/input` or `/kaggle/working`
- copies any bundled `model_bs_roformer*.ckpt` + `.yaml`/`.json` sidecars into the writable
  working dir (Kaggle mounts datasets **read-only**, so the engine cannot use them in place)
- points `STEM_MODEL_DIR` at whatever it found

Prefer a clone? It works only with a token URL:
`git clone https://<PAT>@github.com/vollstek-ops/hrc-studio-stem.git` — set that as `REPO_URL`
in the notebook's first cell.

## 2 · Install (do not let pip swap your torch)

Kaggle images already ship a **CUDA build of PyTorch**. Installing the app's dependencies is
fine as long as pip keeps that torch:

```bash
pip install -q --upgrade-strategy only-if-needed \
    "audio-separator>=0.48.0,<0.50" \
    "soundfile>=0.12.1" "numpy>=1.26" \
    "fastapi>=0.110,<1.0" "uvicorn[standard]>=0.27,<1.0" \
    "python-multipart>=0.0.9" "yt-dlp==2026.10.21"
```

Check the GPU before doing any work:

```python
import torch
from stemkit import separator
print(torch.__version__, torch.version.cuda, torch.cuda.is_available())
print(separator.device_report(refresh=True)["device"])   # must be "cuda"
```

If it prints `cpu`, pip replaced torch with a CPU wheel — restart the session and reinstall
without touching torch. A CPU-only session still works, just roughly 10× slower.

## 3 · Run as an engine (fast path, no UI)

```bash
# one track, best quality
python cli.py /kaggle/input/my-song/track.mp3 --mode 2stem

# a whole album folder, 6 stems, into a chosen output dir
python cli.py /kaggle/input/my-song/*.mp3 --mode 6stem --out /kaggle/working/stems
```

From Python — this is exactly what the notebook does:

```python
from stemkit import separator

result = separator.separate_file(
    "/kaggle/input/my-song/track.mp3",
    "2stem",                 # or "6stem"
    "/kaggle/working/stems",
    quality="studio",        # or "fast"
)
for stem in result["stems"]:
    print(stem["name"], stem["filename"], stem["bit_depth"], stem["sample_rate"])
```

Stems are lossless **24-bit PCM WAV**, named by stem type (`Vocals.wav`, `Instrumental.wav`,
`Drums.wav`, …) regardless of how the model spells them.

**YouTube input** also works when Internet is on:

```python
from stemkit import youtube
path = youtube.download("https://www.youtube.com/watch?v=...", "/kaggle/working/input")["path"]
```

## 4 · Run the studio UI through a tunnel

Kaggle does not expose ports, so bind the app publicly and tunnel it:

```python
import os, subprocess, sys
os.environ.update(STEM_HOST="0.0.0.0", STEM_PORT="8000")
server = subprocess.Popen([sys.executable, "app.py"])
```

Then in a shell cell:

```bash
npx -y localtunnel --port 8000      # prints https://xxxx.loca.lt
# or: cloudflared tunnel --url http://127.0.0.1:8000
# or: ngrok http 8000
```

Open the printed URL and the full DAW-style studio works: transport, A–B loop, per-stem
faders/meters/solo, bounce, ZIP download. The app's job runner spawns each separation in a
subprocess, so a GPU hang on Kaggle is killed and retried instead of bricking the session.

By default the app is single-user with no authentication — the tunnel URL is the only secret,
so do not leave it open longer than you need.

## 5 · Kaggle specifics

| Topic | Detail |
| --- | --- |
| **GPU** | T4 x2 or P100; `device_report()` picks `cuda` and enables fp16 autocast |
| **Quota** | 30 h/week GPU, 12 h per session — a 4-minute track takes well under a minute on a T4 |
| **Disk** | 20 GB; the BS-RoFormer weights are ~610 MB (one-time download), stems are ~15–40 MB per track minute |
| **Persistence** | Only `/kaggle/working` survives a restart; `models/` and `work/` live there when you launch from the repo folder |
| **Internet** | Required for the first model download; disable afterwards to keep sessions hermetic |
| **Read-only input** | `/kaggle/input` cannot be written to — always output to `/kaggle/working` |
| **Long tracks** | On CUDA the full track runs in one pass; the 60 s chunking in this repo exists only to dodge a macOS **MPS** deadlock and is not used on CUDA |

## 6 · Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `device_report()` says `cpu` | pip swapped torch for a CPU wheel — new session, reinstall with `--upgrade-strategy only-if-needed` |
| Model download fails | *Internet* is off, or Hugging Face is blocked in that session — retry, or pre-download the `.ckpt` into `models/` |
| `ffmpeg not found` | Kaggle images include it; if not, `!apt-get -qq install -y ffmpeg` |
| Tunnel URL shows a "click to continue" page | localtunnel's interstitial — pass a custom header or use cloudflared instead |
| Notebook killed mid-job | Session hit the 12 h cap or RAM limit; re-run — finished stems in `/kaggle/working` are still there |
| `The separation process exited unexpectedly` | Read `work/<job_id>/worker.log`; the parent auto-retries a GPU stall twice before failing |

## 7 · Saving your work

`/kaggle/working` is kept when you *Save & Run All* and can be downloaded as notebook output.
To keep stems permanently, download the WAVs from the session, or push them to Google Drive /
Hugging Face with your own token.

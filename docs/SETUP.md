# Setup

Everything here is free and runs locally.

| # | Tool | Why | Check it works |
|---|---|---|---|
| 1 | **Python 3.12 or 3.13** | all pipeline code | `python --version` |
| 2 | **Git** | version control | `git --version` |
| 3 | **Docker Desktop** (or Docker Engine on Linux) | local S3 server (SeaweedFS) and Airflow | `docker run --rm hello-world` |
| 4 | **Astro CLI** | runs Airflow locally in Docker | `astro version` |
| 5 | **Ollama** | runs the local LLM | `ollama --version` |
| 6 | **A local model** (see table below) | the extraction LLM | `ollama run qwen3:8b "say hi"` |

Ingestion alone needs only 1, 2 and 3. Python packages are installed from `requirements-dev.txt`.

## Install commands

**macOS** (Homebrew):
```bash
brew install python@3.13 git
brew install --cask docker          # then open Docker Desktop once
brew install --cask ollama          # or download from https://ollama.com/download
brew install astro
```

**Windows**: install Python from python.org (tick "Add to PATH"), Git for Windows, Docker Desktop
(with WSL 2), Ollama from https://ollama.com/download, and Astro CLI with
`winget install -e --id Astronomer.Astro`. Run the project commands in PowerShell or WSL.

**Linux**: use your package manager for Python and Git, Docker Engine from docs.docker.com, and
`curl -fsSL https://ollama.com/install.sh | sh` for Ollama.

## Which model fits the machine

A model's memory use is roughly its download size plus 1 to 2 GB for the context window
(the pipeline uses about 8k tokens per call). On Apple Silicon, RAM is shared with the GPU, so total
RAM is what counts. On a PC with an NVIDIA GPU, the GPU's own memory (VRAM) decides what runs fast.

| RAM (or VRAM) | Model | Download | Notes |
|---|---|---|---|
| 8 GB | `qwen3:4b` | ~2.5 GB | Works, but expect more invalid JSON; close other apps. |
| **16 GB** | **`qwen3:8b`** | ~5.2 GB | **Default pick.** Good at following a JSON schema. Fallback: `llama3.1:8b`. |
| 32 GB or more | `qwen3:14b` | ~9.3 GB | Noticeably better on long sections; about 2x slower than 8b. |

```bash
ollama pull qwen3:8b
ollama run qwen3:8b "Reply with the JSON {\"ok\": true} and nothing else."
```

Why Qwen3: it has strong structured-output behaviour for its size, and Ollama can constrain its
output to a JSON schema (`format=`), which the extraction step relies on. The evaluation stage
measures its accuracy instead of assuming it (see PROGRESS.md).

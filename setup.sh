#!/bin/bash
set -e

echo "🚀 LOOKS - Vast.ai Setup Script"
echo "Worker ID: $WORKER_ID"

# Aumentar límite de archivos abiertos para descarga de FLUX.2
ulimit -n 65536

# Update system
apt-get update -qq
apt-get install -y git wget curl python3-pip

# Install Python packages
pip install --upgrade pip setuptools wheel
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install transformers accelerate diffusers
pip install supabase requests pillow python-dotenv

# Create workspace
cd /workspace

# Download ComfyUI
echo "📦 Downloading ComfyUI..."
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
pip install -r requirements.txt

# Create model directories
mkdir -p models/unet models/vae models/clip models/checkpoints

# Download FLUX.2 dev FP8 (con autenticación HF)
echo "📥 Downloading FLUX.2 dev FP8..."

# Debug: Verificar que HF_TOKEN existe
if [ -z "$HF_TOKEN" ]; then
    echo "⚠️ HF_TOKEN no configurado - usando método público"
else
    echo "✅ HF_TOKEN configurado (${HF_TOKEN:0:8}...)"
fi

python3 << 'PYTHON_EOF'
from huggingface_hub import hf_hub_download, login
import os

# Obtener token
hf_token = os.getenv('HF_TOKEN', '').strip()

if hf_token and hf_token != '':
    print(f"🔑 Autenticando con HF (token: {hf_token[:8]}...)")
    try:
        login(token=hf_token, add_to_git_credential=False)
        print("✅ Authenticated with Hugging Face")
    except Exception as e:
        print(f"⚠️ Auth error: {e}")
        hf_token = None
else:
    print("⚠️ No HF_TOKEN provided")
    hf_token = None

try:
    print("📥 Downloading FLUX.2-dev...")
    hf_hub_download(
        repo_id="black-forest-labs/FLUX.2-dev",
        filename="flux2-dev.safetensors",  # Nombre correcto (con guion, no guion bajo)
        local_dir="/workspace/ComfyUI/models/checkpoints",
        local_dir_use_symlinks=False,
        token=hf_token if hf_token else None
    )
    print("✅ FLUX.2-dev downloaded successfully")
except Exception as e:
    print(f"❌ FLUX.2-dev failed: {e}")
    print("⚠️ Continuando sin FLUX.2 - worker usará FAL.ai como fallback")
PYTHON_EOF

# Download VAE
echo "📥 Downloading VAE..."
wget -q -O models/vae/ae.safetensors \
  https://huggingface.co/black-forest-labs/FLUX.2-dev/resolve/main/ae.safetensors

# Download CLIP and T5
echo "📥 Downloading encoders..."
wget -q -O models/clip/clip_l.safetensors \
  https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
wget -q -O models/clip/t5xxl_fp8_e4m3fn.safetensors \
  https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors

# Start ComfyUI in background
echo "🎬 Starting ComfyUI..."
cd /workspace/ComfyUI
nohup python main.py --listen 0.0.0.0 --port 8188 > /workspace/comfyui.log 2>&1 &

# Wait for ComfyUI
echo "⏳ Waiting for ComfyUI..."
for i in {1..60}; do
  if curl -s http://127.0.0.1:8188/system_stats > /dev/null 2>&1; then
    echo "✅ ComfyUI ready!"
    break
  fi
  sleep 2
done

# Download and start worker
echo "📥 Downloading worker..."
cd /workspace

if [ ! -z "$GITHUB_REPO" ]; then
  git clone $GITHUB_REPO worker 2>/dev/null || echo "Repo already cloned"
  cd worker
  
  if [ -f worker_vast.py ]; then
    echo "✅ worker_vast.py found in root"
  elif [ -f vast-worker/worker_vast.py ]; then
    echo "✅ worker_vast.py found in vast-worker/"
    cd vast-worker
  fi
  
  if [ -f requirements.txt ]; then
    pip install -r requirements.txt
  fi
  
  echo "🤖 Starting worker..."
  python3 worker_vast.py
else
  echo "❌ GITHUB_REPO not configured"
  tail -f /workspace/comfyui.log
fi

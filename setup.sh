#!/bin/bash
set -e

echo "🚀 LOOKS - Vast.ai Setup Script"
echo "Worker ID: $WORKER_ID"

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

# Download FLUX.2 dev FP8 using Python API
echo "📥 Downloading FLUX.2 dev FP8..."
python3 << 'PYTHON_EOF'
from huggingface_hub import hf_hub_download
try:
    hf_hub_download(
        repo_id="black-forest-labs/FLUX.2-dev",
        filename="flux2_dev_fp8.safetensors",
        local_dir="/workspace/ComfyUI/models/checkpoints",
        local_dir_use_symlinks=False
    )
    print("✅ FLUX.2 downloaded")
except Exception as e:
    print(f"⚠️ Error: {e}")
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

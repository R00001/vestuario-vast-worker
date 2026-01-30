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
# PyTorch 2.4+ necesario para ComfyUI (torch.library.custom_op)
pip install torch==2.4.1 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
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

# Download VAE (~3-4 GB)
echo "📥 Downloading VAE (~3-4 GB)..."
python3 << 'PYTHON_EOF'
from huggingface_hub import hf_hub_download
import os

hf_token = os.getenv('HF_TOKEN', '').strip()

try:
    print("  Downloading ae.safetensors...")
    hf_hub_download(
        repo_id="black-forest-labs/FLUX.2-dev",
        filename="ae.safetensors",
        local_dir="/workspace/ComfyUI/models/vae",
        local_dir_use_symlinks=False,
        token=hf_token if hf_token else None
    )
    print("✅ VAE downloaded")
except Exception as e:
    print(f"❌ VAE failed: {e}")
PYTHON_EOF

# Download CLIP (~2 GB)
echo "📥 Downloading CLIP (~2 GB)..."
python3 << 'PYTHON_EOF'
from huggingface_hub import hf_hub_download
try:
    print("  Downloading clip_l.safetensors...")
    hf_hub_download(
        repo_id="comfyanonymous/flux_text_encoders",
        filename="clip_l.safetensors",
        local_dir="/workspace/ComfyUI/models/clip",
        local_dir_use_symlinks=False
    )
    print("✅ CLIP downloaded")
except Exception as e:
    print(f"❌ CLIP failed: {e}")
PYTHON_EOF

# Download T5 (~4 GB)
echo "📥 Downloading T5 (~4 GB)..."
python3 << 'PYTHON_EOF'
from huggingface_hub import hf_hub_download
try:
    print("  Downloading t5xxl_fp8_e4m3fn.safetensors...")
    hf_hub_download(
        repo_id="comfyanonymous/flux_text_encoders",
        filename="t5xxl_fp8_e4m3fn.safetensors",
        local_dir="/workspace/ComfyUI/models/clip",
        local_dir_use_symlinks=False
    )
    print("✅ T5 downloaded")
except Exception as e:
    print(f"❌ T5 failed: {e}")
PYTHON_EOF

echo "✅ Todos los modelos descargados"

# Start ComfyUI in background
echo "🎬 Starting ComfyUI..."
cd /workspace/ComfyUI
# Puerto 8188 estándar (NO template, imagen PyTorch custom)
nohup python main.py --listen 0.0.0.0 --port 8188 --enable-cors-header > /workspace/comfyui.log 2>&1 &

# Wait for ComfyUI con logs en tiempo real
echo "⏳ Waiting for ComfyUI (port 8188)..."
echo "   Mostrando últimas líneas de comfyui.log cada 10s..."
echo ""

comfy_ready=false
for i in {1..60}; do
  if curl -s http://127.0.0.1:8188/system_stats > /dev/null 2>&1; then
    echo "✅ ComfyUI ready on port 8188!"
    if [ ! -z "$PUBLIC_IPADDR" ]; then
      echo ""
      echo "╔══════════════════════════════════════════════════╗"
      echo "║  ComfyUI URL (acceso externo):"
      echo "║  Busca puerto externo para 8188 en dashboard"
      echo "║  https://cloud.vast.ai/instances/"
      echo "║  URL será: http://$PUBLIC_IPADDR:PUERTO_EXTERNO"
      echo "╚══════════════════════════════════════════════════╝"
      echo ""
    fi
    comfy_ready=true
    break
  fi
  
  # Cada 10 segundos, mostrar últimas líneas del log
  if [ $((i % 5)) -eq 0 ]; then
    echo ""
    echo "━━━ ComfyUI Log (últimas 5 líneas) ━━━"
    tail -5 /workspace/comfyui.log 2>/dev/null || echo "  (log vacío o no existe)"
    echo "━━━ Esperando... ($((i*2))s / 120s) ━━━"
    echo ""
  fi
  
  sleep 2
done

# IMPORTANTE: Verificar que ComfyUI arrancó antes de continuar
if [ "$comfy_ready" = false ]; then
  echo "❌ ComfyUI NO respondió en 120 segundos"
  echo "   Verificando qué pasó..."
  echo ""
  echo "Proceso de ComfyUI:"
  ps aux | grep "python.*main.py" | grep -v grep || echo "  ❌ No se encontró proceso ComfyUI"
  echo ""
  echo "Puerto 8188:"
  netstat -tulpn 2>/dev/null | grep 8188 || echo "  ❌ Puerto 8188 no está abierto"
  echo ""
  echo "Últimas 30 líneas de comfyui.log:"
  tail -30 /workspace/comfyui.log
  echo ""
  echo "⚠️ CONTINUANDO de todos modos - worker intentará conectar..."
fi

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
  echo "   Worker location: $(pwd)"
  echo "   Python version: $(python3 --version)"
  echo "   WORKER_ID: $WORKER_ID"
  echo "   SUPABASE_URL: $SUPABASE_URL"
  echo ""
  echo "════════════════════════════════════════════════════════"
  echo "  WORKER LOGS (también en /var/log/worker-output.log)"
  echo "════════════════════════════════════════════════════════"
  echo ""
  
  # Ejecutar worker - logs van a STDOUT (logs de Vast.ai) Y archivo
  python3 -u worker_vast.py 2>&1 | tee /var/log/worker-output.log
  
  # Si el worker termina, mostrar por qué
  echo ""
  echo "⚠️ Worker terminó inesperadamente"
  echo "Últimas 50 líneas del log:"
  tail -50 /var/log/worker-output.log
else
  echo "❌ GITHUB_REPO not configured"
  tail -f /workspace/comfyui.log
fi

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
pip install torch==2.4.1 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install git+https://github.com/huggingface/diffusers.git
pip install --upgrade transformers accelerate bitsandbytes
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

# FLUX.2-dev YA NO SE DESCARGA (disco lleno)
# Worker usará FAL.ai API que SÍ funciona
echo "✅ Setup completo (worker usa FAL.ai)"

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

#!/bin/bash
# Provisioning script para Vast.ai template
# Se ejecuta DURANTE el provisioning del template

echo "🤖 LOOKS Worker Provisioning"

# 1. Descargar modelos FLUX.2 (el template NO lo hace automáticamente)
echo "📥 Descargando modelos FLUX.2..."

# Asegurar que HF_TOKEN está disponible
if [ -z "$HF_TOKEN" ]; then
    echo "❌ ERROR: HF_TOKEN no configurado"
    exit 1
fi

# Instalar huggingface_hub si no está
pip install -q huggingface-hub

# Descargar modelos a las carpetas de ComfyUI
echo "Descargando FLUX.2-dev UNet..."
huggingface-cli download black-forest-labs/FLUX.2-dev \
    flux2-dev.safetensors \
    --local-dir /workspace/ComfyUI/models/diffusion_models \
    --token $HF_TOKEN

echo "Descargando VAE..."
huggingface-cli download black-forest-labs/FLUX.2-dev \
    ae.safetensors \
    --local-dir /workspace/ComfyUI/models/vae \
    --token $HF_TOKEN

echo "Descargando CLIP-L..."
huggingface-cli download black-forest-labs/FLUX.2-dev \
    clip_l.safetensors \
    --local-dir /workspace/ComfyUI/models/clip \
    --token $HF_TOKEN

echo "Descargando T5XXL..."
huggingface-cli download black-forest-labs/FLUX.2-dev \
    t5xxl_fp8_e4m3fn.safetensors \
    --local-dir /workspace/ComfyUI/models/clip \
    --token $HF_TOKEN

echo "✅ Modelos FLUX.2 descargados"

# 2. Instalar worker
echo "📥 Instalando worker..."
cd /workspace
git clone https://github.com/R00001/vestuario-vast-worker.git worker
cd worker
pip install -r requirements.txt

# Crear servicio supervisor
cat > /etc/supervisor/conf.d/looks-worker.conf << 'SUPERVISOR'
[program:looks-worker]
command=/bin/bash -c 'cd /workspace/worker && COMFYUI_API_BASE=http://localhost:18188 python3 -u worker_vast.py'
environment=WORKER_ID="%(ENV_WORKER_ID)s",SUPABASE_URL="%(ENV_SUPABASE_URL)s",SUPABASE_KEY="%(ENV_SUPABASE_KEY)s",HF_TOKEN="%(ENV_HF_TOKEN)s"
autostart=true
autorestart=true
stderr_logfile=/var/log/looks-worker.err.log
stdout_logfile=/var/log/looks-worker.out.log
SUPERVISOR

# Recargar supervisor
supervisorctl reread
supervisorctl update
supervisorctl start looks-worker

echo "✅ Worker instalado y arrancado"

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

# Instalar huggingface_hub y esperar
echo "Instalando huggingface_hub..."
pip install huggingface_hub
echo "✅ huggingface_hub instalado"

# Descargar modelos con Python
echo "Descargando modelos FLUX.2 (esto tardará ~15-20 min, 64GB)..."
python3 << 'PYTHON'
from huggingface_hub import hf_hub_download
import os

token = os.environ.get('HF_TOKEN')
if not token:
    print("❌ ERROR: HF_TOKEN no disponible")
    exit(1)

print("Descargando FLUX.2-dev UNet...")
hf_hub_download(
    repo_id="black-forest-labs/FLUX.2-dev",
    filename="flux2-dev.safetensors",
    local_dir="/workspace/ComfyUI/models/diffusion_models",
    token=token
)

print("Descargando VAE...")
hf_hub_download(
    repo_id="black-forest-labs/FLUX.2-dev",
    filename="ae.safetensors",
    local_dir="/workspace/ComfyUI/models/vae",
    token=token
)

print("Descargando CLIP-L...")
hf_hub_download(
    repo_id="black-forest-labs/FLUX.2-dev",
    filename="clip_l.safetensors",
    local_dir="/workspace/ComfyUI/models/clip",
    token=token
)

print("Descargando T5XXL...")
hf_hub_download(
    repo_id="black-forest-labs/FLUX.2-dev",
    filename="t5xxl_fp8_e4m3fn.safetensors",
    local_dir="/workspace/ComfyUI/models/clip",
    token=token
)

print("✅ Modelos FLUX.2 descargados")
PYTHON

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

#!/bin/bash
# ============================================================
# LOOKS - Provisioning Script para Template Vast.ai ComfyUI
# 
# GPU: RTX 6000 96GB — carga todos los modelos simultáneamente
# DISCO MÍNIMO: 250GB (modelos: Klein 17GB + LTX base 27GB + Gemma 9GB + LoRAs + template ~20GB)
# Modelos: FLUX Klein 9B + LoRAs (try-on/try-off) + LTX-2.3 (video)
#
# ENV VARS REQUERIDAS en Vast:
#   WORKER_ID, SUPABASE_URL, SUPABASE_KEY, GITHUB_REPO, HF_TOKEN
# ============================================================

set -e

# Silenciar los servicios que spamean "startup paused until provisioning"
# Redirigir sus logs para que no llenen la consola
supervisorctl stop comfyui 2>/dev/null || true
supervisorctl stop api-wrapper 2>/dev/null || true

echo "🚀 [$(date)] LOOKS Provisioning iniciando..."
echo "   Worker ID: $WORKER_ID"
echo "   GitHub Repo: $GITHUB_REPO"
echo "   GPU: 96GB VRAM — todos los modelos residentes"

# Verificar espacio en disco
DISK_AVAIL=$(df -BG /workspace | tail -1 | awk '{print $4}' | sed 's/G//')
echo "   Disco disponible: ${DISK_AVAIL}GB"
if [ "$DISK_AVAIL" -lt 200 ]; then
  echo "⚠️ ADVERTENCIA: Menos de 200GB disponibles (hay ${DISK_AVAIL}GB)."
  echo "   Se necesitan ~120GB para modelos. Configura ≥250GB en Vast."
fi

# Verificar HF_TOKEN
if [ -z "$HF_TOKEN" ]; then
  echo "⚠️ ADVERTENCIA: HF_TOKEN no configurado. Klein 9B requiere autenticación en HuggingFace."
  echo "   Configura HF_TOKEN en las env vars de Vast."
fi

# ============================================================
# PASO 1: Ejecutar el provisioning del template
# Descarga FLUX.2 dev (CLIP, VAE, modelo base)
# Si ya existen los ficheros, se salta
# ============================================================
echo ""
echo "📦 PASO 1: Descargando modelos base del template..."
echo ""

MODELS_DIR="/workspace/ComfyUI/models/diffusion_models"
TEXT_ENCODERS_DIR="/workspace/ComfyUI/models/text_encoders"
VAE_DIR="/workspace/ComfyUI/models/vae"

# Limpiar locks huérfanos que puedan causar "Another process is downloading"
find /workspace/ComfyUI/models -name "*.lock" -delete 2>/dev/null || true
find /tmp -name "*.lock" -delete 2>/dev/null || true

# Solo ejecutar template si faltan modelos base
if [ -f "$VAE_DIR/flux2-vae.safetensors" ] && \
   [ -f "$TEXT_ENCODERS_DIR/mistral_3_small_flux2_bf16.safetensors" ]; then
  echo "   ✓ Modelos base ya existen, saltando template"
else
  TEMPLATE_PROVISIONING="https://github.com/vast-ai/base-image/raw/refs/heads/main/derivatives/pytorch/derivatives/comfyui/provisioning_scripts/flux.2-dev.sh"
  echo "   Descargando script del template..."
  curl -fsSL "$TEMPLATE_PROVISIONING" -o /tmp/flux2-template.sh
  chmod +x /tmp/flux2-template.sh
  echo "   Ejecutando flux.2-dev.sh..."
  /tmp/flux2-template.sh || {
    echo "⚠️ Template falló, continuando..."
  }
fi

echo "✅ [$(date)] Modelos base listos"

# ============================================================
# PASO 1.4: Descargar FLUX Klein 9B + LoRAs
# Klein 9B = base para LoRAs de try-on/try-off (requiere HF_TOKEN)
# ============================================================
echo ""
echo "⚡ PASO 1.4: Descargando FLUX Klein 9B + LoRAs..."
echo ""

MODELS_DIR="/workspace/ComfyUI/models/diffusion_models"
LORAS_DIR="/workspace/ComfyUI/models/loras"
CHECKPOINTS_DIR="/workspace/ComfyUI/models/checkpoints"

mkdir -p "$MODELS_DIR" "$LORAS_DIR" "$CHECKPOINTS_DIR"

# --- FLUX Klein 9B base model (GATED — necesita HF_TOKEN + huggingface-cli) ---
# No sabemos el nombre exacto del .safetensors, huggingface-cli lo descubre
pip install -q huggingface_hub 2>/dev/null

KLEIN_FOUND=$(ls "$MODELS_DIR"/*klein* 2>/dev/null | head -1)
if [ -z "$KLEIN_FOUND" ]; then
  if [ ! -z "$HF_TOKEN" ]; then
    echo "   Descargando FLUX Klein 9B base con huggingface-cli..."
    echo "   (modelo gated, usando HF_TOKEN para autenticación)"
    
    # Descargar TODO el repo Klein — huggingface-cli sabe qué archivos hay
    python3 -c "
import os, glob, shutil
from huggingface_hub import hf_hub_download, list_repo_files

token = os.environ.get('HF_TOKEN')
models_dir = '$MODELS_DIR'

# Probar ambos repos posibles
repos = ['black-forest-labs/FLUX.2-klein-9B', 'black-forest-labs/FLUX.2-klein-base-9B']

for repo in repos:
    try:
        print(f'Probando repo: {repo}')
        files = list_repo_files(repo, token=token)
        safetensors = [f for f in files if f.endswith('.safetensors')]
        print(f'  Archivos safetensors: {safetensors}')
        
        # Buscar el archivo principal del transformer/unet
        # Prioridad: archivo raíz > transformer/ > unet/
        target = None
        for f in safetensors:
            if '/' not in f:  # archivo en raíz
                target = f
                break
        if not target:
            for f in safetensors:
                if 'transformer' in f or 'unet' in f or 'diffusion' in f:
                    target = f
                    break
        if not target and safetensors:
            target = safetensors[0]
        
        if target:
            print(f'  Descargando: {target}')
            path = hf_hub_download(repo, target, local_dir='/tmp/klein_download', token=token)
            # Copiar al directorio de modelos con nombre genérico
            dest = os.path.join(models_dir, 'flux2-klein-9b.safetensors')
            shutil.copy2(path, dest)
            print(f'  ✓ Guardado en: {dest}')
            print(f'  ✓ Tamaño: {os.path.getsize(dest) / 1e9:.1f}GB')
            break
        else:
            print(f'  No safetensors en {repo}')
    except Exception as e:
        print(f'  Error con {repo}: {e}')
        continue
" 2>&1
    
    # Limpiar descarga temporal
    rm -rf /tmp/klein_download 2>/dev/null
    
    # Verificar si descargó y limpiar modelo viejo
    KLEIN_FOUND=$(ls "$MODELS_DIR"/*klein* 2>/dev/null | head -1)
    if [ ! -z "$KLEIN_FOUND" ]; then
      echo "   🗑️ Klein descargado OK. Eliminando flux2_dev_fp8mixed (ahorra 12GB)..."
      rm -f "$MODELS_DIR/flux2_dev_fp8mixed.safetensors" 2>/dev/null || true
    else
      echo "⚠️ Klein 9B no se pudo descargar. Usando FLUX dev como fallback."
    fi
  else
    echo "⚠️ HF_TOKEN no configurado — Klein 9B requiere autenticación"
    echo "   Usando FLUX dev del template como fallback (sin LoRA try-on)"
  fi
else
  echo "   ✓ Klein 9B ya existe: $KLEIN_FOUND"
fi

# --- Try-On LoRA (público, no necesita token) ---
TRYON_LORA="flux-klein-tryon-comfy.safetensors"
if [ ! -f "$LORAS_DIR/$TRYON_LORA" ]; then
  echo "   Descargando Try-On LoRA..."
  cd "$LORAS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/fal/flux-klein-9b-virtual-tryon-lora/resolve/main/flux-klein-tryon-comfy.safetensors" \
    -O "$TRYON_LORA" 2>&1 | tail -n 5 || echo "⚠️ Try-On LoRA no disponible"
  cd /workspace
else
  echo "   ✓ Try-On LoRA ya existe"
fi

# --- Try-Off LoRA (público) ---
TRYOFF_LORA="virtual-tryoff-lora_comfy.safetensors"
if [ ! -f "$LORAS_DIR/$TRYOFF_LORA" ]; then
  echo "   Descargando Try-Off LoRA..."
  cd "$LORAS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/fal/virtual-tryoff-lora/resolve/main/virtual-tryoff-lora_comfy.safetensors" \
    -O "$TRYOFF_LORA" 2>&1 | tail -n 5 || echo "⚠️ Try-Off LoRA no disponible"
  cd /workspace
else
  echo "   ✓ Try-Off LoRA ya existe"
fi

# --- T5-XXL text encoder (Klein 9B usa T5, no Mistral) ---
T5_FILE="t5xxl_fp8_e4m3fn.safetensors"
if [ ! -f "$TEXT_ENCODERS_DIR/$T5_FILE" ]; then
  echo "   Descargando T5-XXL fp8 text encoder (~5GB)..."
  cd "$TEXT_ENCODERS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors" \
    -O "$T5_FILE" 2>&1 | tail -n 5 || echo "⚠️ T5-XXL no disponible"
  cd /workspace
else
  echo "   ✓ T5-XXL ya existe"
fi

echo "✅ [$(date)] Klein 9B + LoRAs + T5-XXL listos"

# ============================================================
# PASO 1.5: Descargar LTX-2.3 para video lookbook
# Necesita: modelo base fp8 (27GB) + Gemma text encoder (8.8GB)
#           + LoRA distilled (7GB) + upscaler (950MB)
# ============================================================
echo ""
echo "🎬 PASO 1.5: Descargando LTX-2.3 para video..."
echo ""

LATENT_UPSCALE_DIR="/workspace/ComfyUI/models/latent_upscale_models"
mkdir -p "$LATENT_UPSCALE_DIR"

# 1. Modelo base LTX-2.3 fp8 (27GB) → checkpoints
if [ ! -f "$CHECKPOINTS_DIR/ltx-2.3-22b-dev-fp8.safetensors" ]; then
  echo "   Descargando LTX-2.3 base fp8 (~27GB)..."
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-22b-dev-fp8.safetensors" \
    -O "$CHECKPOINTS_DIR/ltx-2.3-22b-dev-fp8.safetensors" 2>&1 | tail -n 5 || echo "⚠️ LTX base no disponible"
else
  echo "   ✓ LTX-2.3 base ya existe"
fi

# 2. Gemma 3 text encoder (8.8GB) → text_encoders
if [ ! -f "$TEXT_ENCODERS_DIR/gemma_3_12B_it_fp4_mixed.safetensors" ]; then
  echo "   Descargando Gemma 3 12B text encoder (~8.8GB)..."
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/gemma_3_12B_it_fp4_mixed.safetensors" \
    -O "$TEXT_ENCODERS_DIR/gemma_3_12B_it_fp4_mixed.safetensors" 2>&1 | tail -n 5 || echo "⚠️ Gemma no disponible"
else
  echo "   ✓ Gemma 3 ya existe"
fi

# 3. LoRA distilled (7GB) → loras (NO checkpoints!)
if [ ! -f "$LORAS_DIR/ltx-2.3-22b-distilled-lora-384.safetensors" ]; then
  echo "   Descargando LTX LoRA distilled (~7GB)..."
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-22b-distilled-lora-384.safetensors" \
    -O "$LORAS_DIR/ltx-2.3-22b-distilled-lora-384.safetensors" 2>&1 | tail -n 5 || echo "⚠️ LTX LoRA no disponible"
  # Limpiar si estaba en checkpoints por error
  rm -f "$CHECKPOINTS_DIR/ltx-2.3-22b-distilled-lora-384.safetensors" 2>/dev/null
else
  echo "   ✓ LTX LoRA distilled ya existe"
fi

# 4. Spatial upscaler (950MB) → latent_upscale_models
if [ ! -f "$LATENT_UPSCALE_DIR/ltx-2.3-spatial-upscaler-x2-1.0.safetensors" ]; then
  echo "   Descargando LTX spatial upscaler (~950MB)..."
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-spatial-upscaler-x2-1.0.safetensors" \
    -O "$LATENT_UPSCALE_DIR/ltx-2.3-spatial-upscaler-x2-1.0.safetensors" 2>&1 | tail -n 5 || echo "⚠️ Upscaler no disponible"
else
  echo "   ✓ LTX upscaler ya existe"
fi

echo "✅ [$(date)] LTX-2.3 completo (base + Gemma + LoRA + upscaler)"

# ============================================================
# PASO 1.6: Instalar Custom Nodes
# ============================================================
echo ""
echo "🔧 PASO 1.6: Instalando Custom Nodes..."
echo ""

cd /workspace/ComfyUI/custom_nodes

# ComfyUI-FLUX (ReferenceLatent, FluxKontextImageScale, etc.)
if [ ! -d "ComfyUI-FLUX" ]; then
  echo "   Instalando ComfyUI-FLUX..."
  git clone https://github.com/city96/ComfyUI-FLUX.git 2>/dev/null || echo "   no disponible"
fi

# ComfyUI-KJNodes
if [ ! -d "ComfyUI-KJNodes" ]; then
  echo "   Instalando ComfyUI-KJNodes..."
  git clone https://github.com/kijai/ComfyUI-KJNodes.git 2>/dev/null || echo "   no disponible"
fi

# ComfyUI-Custom-Scripts
if [ ! -d "ComfyUI-Custom-Scripts" ]; then
  echo "   Instalando ComfyUI-Custom-Scripts..."
  git clone https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git 2>/dev/null || echo "   no disponible"
fi

# ComfyUI-LTXVideo (nodos para LTX-2.3 image-to-video)
if [ ! -d "ComfyUI-LTXVideo" ]; then
  echo "   Instalando ComfyUI-LTXVideo..."
  git clone https://github.com/Lightricks/ComfyUI-LTXVideo.git 2>/dev/null || {
    echo "   Probando repo kijai..."
    git clone https://github.com/kijai/ComfyUI-LTXVideo.git 2>/dev/null || echo "   no disponible"
  }
fi

# ComfyUI-VideoHelperSuite (guardar videos como mp4)
if [ ! -d "ComfyUI-VideoHelperSuite" ]; then
  echo "   Instalando ComfyUI-VideoHelperSuite..."
  git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git 2>/dev/null || echo "   no disponible"
fi

# Instalar dependencias de custom nodes
for dir in */; do
  if [ -f "${dir}requirements.txt" ]; then
    echo "   Instalando deps para ${dir}..."
    pip install -r "${dir}requirements.txt" 2>/dev/null || true
  fi
done

# Forzar opencv-python-headless DESPUÉS de todo (requirements pueden instalar opencv-python que falla sin libGL)
pip uninstall -y opencv-python 2>/dev/null || true
pip install -q opencv-python-headless imageio-ffmpeg 2>/dev/null || true
echo "   ✓ cv2 (headless) + ffmpeg instalados"

cd /workspace

echo "✅ [$(date)] Custom nodes instalados"

# Listar todo para debug
echo ""
echo "📋 Modelos disponibles:"
echo "   diffusion_models:"
ls -lh "$MODELS_DIR"/*.safetensors 2>/dev/null || echo "     (ninguno)"
echo "   loras:"
ls -lh "$LORAS_DIR"/*.safetensors 2>/dev/null || echo "     (ninguno)"
echo "   checkpoints:"
ls -lh "$CHECKPOINTS_DIR"/*.safetensors 2>/dev/null || echo "     (ninguno)"
echo "   custom_nodes:"
ls -d /workspace/ComfyUI/custom_nodes/*/ 2>/dev/null || echo "     (ninguno)"
echo ""
echo "   Disco usado:"
df -h /workspace | tail -1
echo ""

# ============================================================
# PASO 2: Instalar nuestro worker
# ============================================================
echo ""
echo "📥 PASO 2: Instalando LOOKS Worker..."
echo ""

cd /workspace

if [ ! -z "$GITHUB_REPO" ]; then
  if [ ! -d "worker" ]; then
    echo "   Clonando repo: $GITHUB_REPO"
    git clone "$GITHUB_REPO" worker
  else
    echo "   Worker ya existe, actualizando..."
    cd worker && git pull && cd ..
  fi
  
  cd worker
  if [ -f requirements.txt ]; then
    echo "   Instalando dependencias..."
    pip install -r requirements.txt
  fi
  cd /workspace
else
  echo "⚠️ GITHUB_REPO no configurado"
fi

# ============================================================
# PASO 2.5: Verificar CUDA + Configurar ComfyUI --highvram
# ============================================================
echo ""
echo "🔥 PASO 2.5: Verificando CUDA y configurando ComfyUI --highvram..."
echo ""

# Esperar CUDA
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
  if python3 -c "import torch; exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    echo "   ✓ CUDA: $(python3 -c 'import torch; print(torch.cuda.get_device_name(0))' 2>/dev/null)"
    echo "   ✓ VRAM: $(python3 -c 'import torch; print(f\"{torch.cuda.get_device_properties(0).total_mem/1e9:.0f}GB\")' 2>/dev/null)"
    break
  fi
  sleep 2
  WAITED=$((WAITED + 2))
done

# Añadir --highvram a ComfyUI (mantener todos los modelos en 96GB VRAM)
if [ -f /etc/supervisor/conf.d/comfyui.conf ]; then
  echo "   Añadiendo --highvram a ComfyUI..."
  sed -i 's/main\.py/main.py --highvram/g' /etc/supervisor/conf.d/comfyui.conf 2>/dev/null || true
  sed -i 's/--highvram --highvram/--highvram/g' /etc/supervisor/conf.d/comfyui.conf 2>/dev/null || true
fi

# NO reiniciar ComfyUI aquí — arrancará solo cuando provisioning termine
# (el fichero /.provisioning bloquea ComfyUI hasta que este script acabe)
# El worker tiene su propio bucle de espera en main_loop()
echo "   ComfyUI arrancará con --highvram cuando provisioning termine"

echo "✅ [$(date)] ComfyUI configurado"

# ============================================================
# PASO 3: Configurar worker como servicio supervisor
# ============================================================
echo ""
echo "⚙️ PASO 3: Configurando worker como servicio..."
echo ""

cat > /etc/supervisor/conf.d/looks-worker.conf << 'SUPERVISOR_EOF'
[program:looks-worker]
command=/bin/bash -c 'cd /workspace/worker && while [ -f /.provisioning ]; do echo "Esperando provisioning..." && sleep 5; done && while ! curl -s http://localhost:18188/system_stats > /dev/null 2>&1; do echo "Esperando ComfyUI..." && sleep 5; done && COMFYUI_API_BASE=http://localhost:18188 python3 -u worker_vast.py'
environment=WORKER_ID="%(ENV_WORKER_ID)s",SUPABASE_URL="%(ENV_SUPABASE_URL)s",SUPABASE_KEY="%(ENV_SUPABASE_KEY)s",HF_TOKEN="%(ENV_HF_TOKEN)s"
directory=/workspace/worker
autostart=true
autorestart=true
startsecs=5
startretries=999
stderr_logfile=/var/log/looks-worker.err.log
stdout_logfile=/var/log/looks-worker.out.log
SUPERVISOR_EOF

supervisorctl reread
supervisorctl update

# Rearrancar ComfyUI (lo paramos al inicio para quitar spam)
echo "   Rearrancando ComfyUI..."
supervisorctl start comfyui 2>/dev/null || true
supervisorctl start api-wrapper 2>/dev/null || true

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅ LOOKS Provisioning completado!                       ║"
echo "║  GPU: 96GB VRAM — --highvram activo                     ║"
echo "║  Modelos: Klein 9B + LoRAs + LTX-2.3                    ║"
echo "║  Disco: $(df -BG /workspace | tail -1 | awk '{print $3"/"$2}') usado                              ║"
echo "║                                                          ║"
echo "║  Logs:                                                   ║"
echo "║    • Worker: tail -f /var/log/looks-worker.out.log      ║"
echo "║    • ComfyUI: tail -f /workspace/ComfyUI/user/comfyui.log║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "🎉 [$(date)] LOOKS Provisioning finalizado!"

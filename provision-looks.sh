#!/bin/bash
# ============================================================
# LOOKS - Provisioning Script para Template Vast.ai ComfyUI
# 
# GPU: RTX 6000 96GB — carga todos los modelos simultáneamente
# Modelos: FLUX Klein 9B + LoRAs (try-on/try-off) + LTX-2.3 (video)
#
# IMPORTANTE: Este script se usa como PROVISIONING_SCRIPT
# y debe INCLUIR el provisioning del template + nuestro worker
# ============================================================

set -e
echo "🚀 [$(date)] LOOKS Provisioning iniciando..."
echo "   Worker ID: $WORKER_ID"
echo "   GitHub Repo: $GITHUB_REPO"
echo "   GPU: 96GB VRAM — todos los modelos residentes"

# ============================================================
# PASO 1: Ejecutar el provisioning del template
# El template vastai/comfy descarga base models y ComfyUI
# ============================================================
echo ""
echo "📦 PASO 1: Ejecutando provisioning del template (FLUX.2)..."
echo "   Esto puede tardar 15-20 minutos..."
echo ""

TEMPLATE_PROVISIONING="https://github.com/vast-ai/base-image/raw/refs/heads/main/derivatives/pytorch/derivatives/comfyui/provisioning_scripts/flux.2-dev.sh"

# Descargar y ejecutar el script del template
echo "   Descargando: $TEMPLATE_PROVISIONING"
curl -fsSL "$TEMPLATE_PROVISIONING" -o /tmp/flux2-template.sh
chmod +x /tmp/flux2-template.sh

echo "   Ejecutando flux.2-dev.sh del template..."
/tmp/flux2-template.sh || {
  echo "⚠️ Provisioning del template falló, continuando..."
}

echo "✅ [$(date)] Provisioning del template completado"

# ============================================================
# PASO 1.4: Descargar FLUX Klein 9B (reemplaza FLUX dev)
# Klein 9B = base para LoRAs de try-on/try-off
# Con 96GB usamos bf16 nativo (máxima calidad)
# ============================================================
echo ""
echo "⚡ PASO 1.4: Descargando FLUX Klein 9B (base para try-on LoRAs)..."
echo "   Klein 9B bf16 = ~18GB — cabe de sobra en 96GB VRAM"
echo ""

MODELS_DIR="/workspace/ComfyUI/models/diffusion_models"
LORAS_DIR="/workspace/ComfyUI/models/loras"
CHECKPOINTS_DIR="/workspace/ComfyUI/models/checkpoints"
TEXT_ENCODERS_DIR="/workspace/ComfyUI/models/text_encoders"

mkdir -p "$MODELS_DIR" "$LORAS_DIR" "$CHECKPOINTS_DIR" "$TEXT_ENCODERS_DIR"

# --- FLUX Klein 9B base model ---
KLEIN_MODEL="flux2-klein-9b.safetensors"
if [ ! -f "$MODELS_DIR/$KLEIN_MODEL" ]; then
  echo "   Descargando FLUX Klein 9B base (~18GB bf16)..."
  cd "$MODELS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/black-forest-labs/FLUX.2-klein-9B/resolve/main/flux2-klein-9b.safetensors" \
    -O "$KLEIN_MODEL" 2>&1 | tail -n 5 || {
    echo "⚠️ Error descargando Klein 9B, probando nombre alternativo..."
    wget --progress=bar:force:noscroll \
      "https://huggingface.co/black-forest-labs/FLUX.2-klein-base-9B/resolve/main/flux2-klein-base-9b.safetensors" \
      -O "$KLEIN_MODEL" 2>&1 | tail -n 5 || {
      echo "⚠️ Klein 9B no disponible, manteniendo FLUX dev del template"
    }
  }
  cd /workspace
else
  echo "   ✓ Klein 9B ya existe"
fi

# --- Try-On LoRA (ComfyUI version) ---
TRYON_LORA="flux-klein-tryon-comfy.safetensors"
if [ ! -f "$LORAS_DIR/$TRYON_LORA" ]; then
  echo "   Descargando Try-On LoRA (~500MB)..."
  cd "$LORAS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/fal/flux-klein-9b-virtual-tryon-lora/resolve/main/flux-klein-tryon-comfy.safetensors" \
    -O "$TRYON_LORA" 2>&1 | tail -n 5 || {
    echo "⚠️ Try-On LoRA comfy no encontrado, probando versión diffusers..."
    wget --progress=bar:force:noscroll \
      "https://huggingface.co/fal/flux-klein-9b-virtual-tryon-lora/resolve/main/flux-klein-tryon.safetensors" \
      -O "flux-klein-tryon.safetensors" 2>&1 | tail -n 5
  }
  cd /workspace
else
  echo "   ✓ Try-On LoRA ya existe"
fi

# --- Try-Off LoRA (ComfyUI version) ---
TRYOFF_LORA="virtual-tryoff-lora_comfy.safetensors"
if [ ! -f "$LORAS_DIR/$TRYOFF_LORA" ]; then
  echo "   Descargando Try-Off LoRA (~500MB)..."
  cd "$LORAS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/fal/virtual-tryoff-lora/resolve/main/virtual-tryoff-lora_comfy.safetensors" \
    -O "$TRYOFF_LORA" 2>&1 | tail -n 5 || {
    echo "⚠️ Try-Off LoRA comfy no encontrado, probando diffusers..."
    wget --progress=bar:force:noscroll \
      "https://huggingface.co/fal/virtual-tryoff-lora/resolve/main/virtual-tryoff-lora_diffusers.safetensors" \
      -O "virtual-tryoff-lora.safetensors" 2>&1 | tail -n 5
  }
  cd /workspace
else
  echo "   ✓ Try-Off LoRA ya existe"
fi

echo "✅ [$(date)] Klein 9B + LoRAs listos"

# ============================================================
# PASO 1.5: Descargar LTX-2.3 para video lookbook
# ============================================================
echo ""
echo "🎬 PASO 1.5: Descargando LTX-2.3 para video generation..."
echo "   LTX-2.3 bf16 = ~15-20GB — cargado permanentemente en 96GB"
echo ""

# LTX-2.3 22B distilled (8 steps = ~6x más rápido que dev)
# 22B params = ~44GB bf16 — cabe en 96GB junto con Klein
# El modelo puede venir como un solo .safetensors o como directorio
LTX_MODEL="ltx-2.3-22b-distilled.safetensors"
if [ ! -f "$CHECKPOINTS_DIR/$LTX_MODEL" ]; then
  echo "   Descargando LTX-2.3 22B distilled (~44GB bf16)..."
  echo "   Este es el modelo de video — 8 steps, máxima velocidad"
  cd "$CHECKPOINTS_DIR"
  # Intentar descarga directa del safetensors
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-22b-distilled.safetensors" \
    -O "$LTX_MODEL" 2>&1 | tail -n 5 || {
    echo "   Probando nombre alternativo..."
    wget --progress=bar:force:noscroll \
      "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx2_3_distilled.safetensors" \
      -O "$LTX_MODEL" 2>&1 | tail -n 5 || {
      echo "   Probando con huggingface-cli..."
      pip install -q huggingface_hub 2>/dev/null
      python3 -c "
from huggingface_hub import hf_hub_download, list_repo_files
files = list_repo_files('Lightricks/LTX-2.3')
# Buscar el archivo distilled
distilled = [f for f in files if 'distilled' in f.lower() and f.endswith('.safetensors')]
print(f'Archivos distilled: {distilled}')
if distilled:
    hf_hub_download('Lightricks/LTX-2.3', distilled[0], local_dir='$CHECKPOINTS_DIR')
    print(f'Descargado: {distilled[0]}')
else:
    # Fallback: descargar lo que haya
    all_st = [f for f in files if f.endswith('.safetensors')]
    print(f'Todos safetensors: {all_st}')
" 2>&1 || echo "⚠️ LTX-2.3 no disponible"
    }
  }
  cd /workspace
else
  echo "   ✓ LTX-2.3 ya existe"
fi

echo "✅ [$(date)] LTX-2.3 + T5 text encoder listos"

# ============================================================
# PASO 1.6: Instalar Custom Nodes
# ============================================================
echo ""
echo "🔧 PASO 1.6: Instalando Custom Nodes..."
echo ""

cd /workspace/ComfyUI/custom_nodes

# ComfyUI-FLUX (contiene ReferenceLatent, FluxKontextImageScale, etc.)
if [ ! -d "ComfyUI-FLUX" ]; then
  echo "   Instalando ComfyUI-FLUX..."
  git clone https://github.com/city96/ComfyUI-FLUX.git 2>/dev/null || echo "   ComfyUI-FLUX no disponible"
fi

# ComfyUI-KJNodes (utilidades adicionales)
if [ ! -d "ComfyUI-KJNodes" ]; then
  echo "   Instalando ComfyUI-KJNodes..."
  git clone https://github.com/kijai/ComfyUI-KJNodes.git 2>/dev/null || echo "   ComfyUI-KJNodes no disponible"
fi

# ComfyUI-Custom-Scripts (utilidades)
if [ ! -d "ComfyUI-Custom-Scripts" ]; then
  echo "   Instalando ComfyUI-Custom-Scripts..."
  git clone https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git 2>/dev/null || echo "   Custom-Scripts no disponible"
fi

# ComfyUI-LTXVideo (nodos para LTX-2.3 image-to-video)
if [ ! -d "ComfyUI-LTXVideo" ]; then
  echo "   Instalando ComfyUI-LTXVideo..."
  git clone https://github.com/Lightricks/ComfyUI-LTXVideo.git 2>/dev/null || {
    echo "   Probando repo alternativo kijai..."
    git clone https://github.com/kijai/ComfyUI-LTXVideo.git 2>/dev/null || echo "   ComfyUI-LTXVideo no disponible"
  }
fi

# ComfyUI-VideoHelperSuite (para guardar videos como mp4)
if [ ! -d "ComfyUI-VideoHelperSuite" ]; then
  echo "   Instalando ComfyUI-VideoHelperSuite..."
  git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git 2>/dev/null || echo "   VideoHelperSuite no disponible"
fi

# Instalar dependencias de TODOS los custom nodes
for dir in */; do
  if [ -f "${dir}requirements.txt" ]; then
    echo "   Instalando deps para ${dir}..."
    pip install -r "${dir}requirements.txt" 2>/dev/null || true
  fi
done

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
echo "   text_encoders:"
ls -lh "$TEXT_ENCODERS_DIR"/*.safetensors 2>/dev/null || echo "     (ninguno)"
echo "   custom_nodes:"
ls -d /workspace/ComfyUI/custom_nodes/*/ 2>/dev/null || echo "     (ninguno)"
echo ""

# ============================================================
# PASO 2: Instalar nuestro worker
# ============================================================
echo ""
echo "📥 PASO 2: Instalando LOOKS Worker..."
echo ""

cd /workspace

# Clonar el worker si no existe
if [ ! -z "$GITHUB_REPO" ]; then
  if [ ! -d "worker" ]; then
    echo "   Clonando repo: $GITHUB_REPO"
    git clone "$GITHUB_REPO" worker
  else
    echo "   Worker ya existe, actualizando..."
    cd worker && git pull && cd ..
  fi
  
  cd worker
  
  # Instalar dependencias
  if [ -f requirements.txt ]; then
    echo "   Instalando dependencias..."
    pip install -r requirements.txt
  fi
  
  cd /workspace
else
  echo "⚠️ GITHUB_REPO no configurado, saltando instalación de worker"
fi

# ============================================================
# PASO 2.5: Verificar CUDA + Configurar ComfyUI --highvram
# ============================================================
echo ""
echo "🔥 PASO 2.5: Verificando CUDA y configurando ComfyUI --highvram..."
echo ""

# Esperar a que CUDA esté disponible
echo "   Esperando CUDA disponible..."
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
  if python3 -c "import torch; exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    echo "   ✓ CUDA disponible: $(python3 -c 'import torch; print(torch.cuda.get_device_name(0))' 2>/dev/null)"
    echo "   ✓ VRAM: $(python3 -c 'import torch; print(f\"{torch.cuda.get_device_properties(0).total_mem/1e9:.0f}GB\")' 2>/dev/null)"
    break
  fi
  sleep 2
  WAITED=$((WAITED + 2))
  echo "   Esperando CUDA... ($WAITED/$MAX_WAIT seg)"
done

if [ $WAITED -ge $MAX_WAIT ]; then
  echo "   ⚠️ CUDA no disponible después de $MAX_WAIT segundos, continuando..."
fi

# Modificar el arranque de ComfyUI para usar --highvram
# Esto mantiene TODOS los modelos cargados en 96GB VRAM simultáneamente
if [ -f /etc/supervisor/conf.d/comfyui.conf ]; then
  echo "   Añadiendo --highvram a ComfyUI supervisor config..."
  sed -i 's/main\.py/main.py --highvram/g' /etc/supervisor/conf.d/comfyui.conf 2>/dev/null || true
  # Verificar que no dupliquemos el flag
  sed -i 's/--highvram --highvram/--highvram/g' /etc/supervisor/conf.d/comfyui.conf 2>/dev/null || true
fi

# Reiniciar ComfyUI con --highvram para que detecte GPU correctamente
echo "   Reiniciando ComfyUI con --highvram..."
supervisorctl stop comfyui 2>/dev/null || true
sleep 3
supervisorctl start comfyui 2>/dev/null || true

# Esperar a que ComfyUI esté listo
echo "   Esperando ComfyUI con --highvram..."
MAX_WAIT=180
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
  if curl -s http://localhost:18188/system_stats > /dev/null 2>&1; then
    echo "   ✓ ComfyUI listo con --highvram (96GB, modelos residentes)"
    break
  fi
  sleep 5
  WAITED=$((WAITED + 5))
done

echo "✅ [$(date)] ComfyUI configurado con --highvram"

# ============================================================
# PASO 3: Configurar worker como servicio supervisor
# ============================================================
echo ""
echo "⚙️ PASO 3: Configurando worker como servicio supervisor..."
echo ""

# Crear config de supervisor para el worker
cat > /etc/supervisor/conf.d/looks-worker.conf << 'SUPERVISOR_EOF'
[program:looks-worker]
command=/bin/bash -c 'cd /workspace/worker && while [ -f /.provisioning ]; do echo "Esperando provisioning..." && sleep 5; done && while ! curl -s http://localhost:18188/system_stats > /dev/null 2>&1; do echo "Esperando ComfyUI..." && sleep 5; done && COMFYUI_API_BASE=http://localhost:18188 python3 -u worker_vast.py'
environment=WORKER_ID="%(ENV_WORKER_ID)s",SUPABASE_URL="%(ENV_SUPABASE_URL)s",SUPABASE_KEY="%(ENV_SUPABASE_KEY)s"
directory=/workspace/worker
autostart=true
autorestart=true
startsecs=5
startretries=999
stderr_logfile=/var/log/looks-worker.err.log
stdout_logfile=/var/log/looks-worker.out.log
SUPERVISOR_EOF

# Recargar supervisor para incluir el nuevo servicio
echo "   Recargando supervisor..."
supervisorctl reread
supervisorctl update

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅ LOOKS Provisioning completado!                       ║"
echo "║                                                          ║"
echo "║  GPU: 96GB VRAM — --highvram activo                     ║"
echo "║  Modelos: Klein 9B + LoRAs + LTX-2.3 (todos cargados)  ║"
echo "║                                                          ║"
echo "║  El worker esperará a que ComfyUI esté listo y luego    ║"
echo "║  empezará a procesar jobs automáticamente.              ║"
echo "║                                                          ║"
echo "║  Logs:                                                   ║"
echo "║    • Worker: tail -f /var/log/looks-worker.out.log      ║"
echo "║    • ComfyUI: tail -f /workspace/ComfyUI/user/comfyui.log║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "🎉 [$(date)] LOOKS Provisioning finalizado!"

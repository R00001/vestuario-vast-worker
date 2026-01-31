#!/bin/bash
# ============================================================
# LOOKS - Provisioning Script para Template Vast.ai ComfyUI
# 
# IMPORTANTE: Este script se usa como PROVISIONING_SCRIPT
# y debe INCLUIR el provisioning del template + nuestro worker
# ============================================================

set -e
echo "🚀 [$(date)] LOOKS Provisioning iniciando..."
echo "   Worker ID: $WORKER_ID"
echo "   GitHub Repo: $GITHUB_REPO"

# ============================================================
# PASO 1: Ejecutar el provisioning del template
# El template vastai/comfy usa flux.2-dev.sh para descargar modelos
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
echo "║  El worker esperará a que ComfyUI esté listo y luego    ║"
echo "║  empezará a procesar jobs automáticamente.              ║"
echo "║                                                          ║"
echo "║  Logs:                                                   ║"
echo "║    • Worker: tail -f /var/log/looks-worker.out.log      ║"
echo "║    • ComfyUI: tail -f /workspace/ComfyUI/user/comfyui.log║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "🎉 [$(date)] LOOKS Provisioning finalizado!"

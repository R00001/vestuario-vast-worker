#!/bin/bash
# Provisioning script para Vast.ai template con FLUX.2
# El template YA descarga los modelos, solo instalamos el worker

echo "🤖 LOOKS Worker Provisioning"
echo "Template descargará FLUX.2, instalando worker..."

# Instalar worker
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

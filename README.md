# 🚀 Vast.ai GPU Worker para LOOKS

Worker que corre en instancias GPU alquiladas de Vast.ai para procesar try-ons con FLUX.2 dev.

---

## 📋 ¿Qué es esto?

Este worker:
- Corre **dentro de una instancia GPU** alquilada en Vast.ai
- Consume jobs de Supabase marcados con `preferred_backend='vast'`
- Ejecuta ComfyUI + FLUX.2 dev localmente en la GPU
- Sube resultados a Supabase Storage
- Se gestiona automáticamente desde tu backend Node.js

---

## 🏗️ Arquitectura

```
Tu Servidor (Node.js)
    ↓ [alquila GPU via Vast API]
Vast.ai crea instancia RTX 6000
    ↓ [ejecuta onstart script]
Container descarga este worker desde GitHub
    ↓ [ejecuta worker_vast.py]
Worker hace polling a Supabase
    ↓ [encuentra jobs con backend='vast']
Worker ejecuta ComfyUI
    ↓ [genera try-on]
Worker sube resultado a Storage
    ↓
Job marcado como completed
```

---

## 📁 Archivos

| Archivo | Descripción |
|---------|-------------|
| `worker_vast.py` | Worker principal que corre en GPU |
| `requirements.txt` | Dependencies Python |
| `workflows/tryon_template.json` | Workflow ComfyUI (TODO) |
| `README.md` | Esta documentación |

---

## 🔧 Setup (se ejecuta automáticamente)

El `onstart` script en `vast-manager.js` hace:

1. **Instalar Python packages:**
   - PyTorch + CUDA 12.4
   - Supabase SDK
   - Requests, Pillow

2. **Descargar ComfyUI:**
   - Git clone
   - Install requirements
   - Custom nodes (IPAdapter, etc.)

3. **Descargar FLUX.2 dev FP8:**
   - ~24 GB modelo optimizado
   - VAE, CLIP, T5 encoders

4. **Iniciar ComfyUI:**
   - Server en puerto 8188
   - API REST disponible

5. **Ejecutar worker_vast.py:**
   - Polling Supabase cada 5s
   - Procesa hasta 12 jobs batch

**Tiempo total setup:** ~10-15 minutos

---

## ⚙️ Variables de Entorno

El backend las configura automáticamente al alquilar:

```bash
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_KEY=service_key_aqui
WORKER_ID=vast-worker-1234567890
GITHUB_REPO=https://github.com/tu-usuario/vestuario.git
```

---

## 🔄 Funcionamiento

### Loop Principal:

1. **Heartbeat** (cada 30s):
   - Actualiza `vast_instances.last_health_check`
   - Marca como `health_status='healthy'`

2. **Polling** (cada 5s):
   - Busca jobs con `status='pending' AND preferred_backend='vast'`
   - Máximo 12 jobs (batch)

3. **Procesamiento:**
   - Descarga avatar + prendas
   - Construye prompt
   - Ejecuta ComfyUI workflow
   - Sube resultado a Storage
   - Actualiza job a `completed`

4. **Idle Detection:**
   - Si no hay jobs >30 min → Backend destruye la GPU

---

## 📊 Límites Configurados

| Límite | Valor | Dónde se controla |
|--------|-------|-------------------|
| Max GPUs concurrentes | 4 | `vast-manager.js` |
| Max precio/hora | $1.30 | `vast-manager.js` |
| Horario permitido | 9am-1am España | `vast-manager.js` + `ai-orchestrator.js` |
| Auto-shutdown idle | 30 min | `vast-manager.js` |
| Max batch size | 12 jobs | `worker_vast.py` |
| Min batch size | 1 job (FCFS) | `worker_vast.py` |

---

## 🐛 Debugging

### Ver logs del worker:

Si tienes acceso SSH a la instancia:
```bash
# Conectar via SSH (Vast.ai te da el comando)
ssh -p PUERTO root@IP

# Ver logs
tail -f /workspace/worker.log

# Ver logs ComfyUI
tail -f /workspace/comfyui.log

# Estado de procesos
ps aux | grep python
```

### Ver estado en Supabase:

```sql
-- Ver instancias activas
SELECT * FROM vast_instances WHERE status IN ('ready', 'busy', 'idle');

-- Ver jobs en Vast
SELECT * FROM ai_generation_jobs WHERE preferred_backend='vast' AND status='pending';

-- Stats
SELECT * FROM get_vast_stats();
```

---

## 💰 Costos

**Por job:**
- FAL.ai: $0.02-$0.06
- Vast.ai: ~$0.005 (incluye alquiler GPU prorrateado)

**Por hora:**
- RTX 6000: ~$0.90-$1.30
- Procesa ~144 jobs/hora (12 batch cada 5 min)
- Costo por job: $0.90/144 = $0.0062

**Ahorro:** 54% vs FAL.ai puro

---

## ✅ Checklist de Deploy

- [ ] Crear cuenta Vast.ai
- [ ] Obtener API key
- [ ] Configurar `VAST_API_KEY` en backend
- [ ] Subir `vast-worker/` a GitHub
- [ ] Ejecutar `schema_vast_instances.sql` en Supabase
- [ ] Probar alquiler manual de 1 GPU
- [ ] Verificar worker_vast.py se descarga y ejecuta
- [ ] Probar 1 try-on end-to-end
- [ ] Configurar monitoring (Datadog/Sentry)
- [ ] Deploy a producción

---

## 🚨 Troubleshooting

**Worker no arranca:**
- Verificar ComfyUI logs: `tail -f /workspace/comfyui.log`
- Verificar SUPABASE_URL y KEY están configuradas
- Verificar GitHub repo es accesible

**Jobs no se procesan:**
- Verificar worker_id en `vast_instances` tabla
- Verificar jobs tienen `preferred_backend='vast'`
- Ver logs de worker: `tail -f /workspace/worker.log`

**GPU se destruye muy rápido:**
- Verificar `auto_shutdown_idle_minutes` (default: 30)
- Ver `vast_instances.force_shutdown_reason`
- Ajustar límites en `vast-manager.js`

---

**Mantenido por:** LOOKS Team  
**Última actualización:** 2026-01-29

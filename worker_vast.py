#!/usr/bin/env python3
"""
LOOKS - Vast.ai GPU Worker
Corre EN la instancia Vast alquilada
Consume jobs de Supabase y ejecuta ComfyUI
"""

import os
import sys
import time
import json
import requests
from datetime import datetime
from supabase import create_client, Client
import base64
from pathlib import Path

# ============================================
# CONFIGURACIÓN
# ============================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WORKER_ID = os.getenv("WORKER_ID", f"vast-worker-{int(time.time())}")

# Template vastai/comfy: puerto 18188
COMFY_URL = os.getenv("COMFYUI_API_BASE", "http://127.0.0.1:18188")

WORKER_CONFIG = {
    'POLL_INTERVAL_SECONDS': 5,      # Polling cada 5s
    'MAX_BATCH_SIZE': 12,            # Máximo 12 jobs simultáneos
    'MIN_BATCH_SIZE': 1,             # Mínimo 1 (FCFS)
    'JOB_TIMEOUT_SECONDS': 300,      # Timeout 5 minutos
    'HEARTBEAT_INTERVAL_SECONDS': 30, # Heartbeat cada 30s
}

print(f"""
╔═══════════════════════════════════════════════╗
║  LOOKS - Vast.ai GPU Worker                   ║
║  Worker ID: {WORKER_ID:31s} ║
║  ComfyUI: {COMFY_URL:34s} ║
╚═══════════════════════════════════════════════╝
""")

# Verificar variables de entorno
if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ ERROR: SUPABASE_URL o SUPABASE_KEY no configurados")
    sys.exit(1)

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================
# FUNCIONES AUXILIARES
# ============================================

def check_comfy_ready():
    """Verificar que ComfyUI esté listo"""
    try:
        resp = requests.get(f"{COMFY_URL}/system_stats", timeout=5)
        return resp.status_code == 200
    except:
        return False

def download_image(url, local_path):
    """Descargar imagen de URL a filesystem local"""
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        
        with open(local_path, 'wb') as f:
            f.write(resp.content)
        
        return local_path
    except Exception as e:
        print(f"❌ Error descargando {url}: {e}")
        raise

def upload_to_storage(job_id, user_id, image_base64):
    """Subir imagen a Supabase Storage"""
    try:
        import io
        
        # Decodificar base64
        image_bytes = base64.b64decode(image_base64)
        
        # Nombre del archivo
        filename = f"tryon_{user_id}_{job_id}_{int(time.time())}.jpg"
        filepath = f"{user_id}/tryons/{filename}"
        
        # Subir a Supabase
        result = supabase.storage.from_('avatars').upload(
            filepath,
            image_bytes,
            file_options={"content-type": "image/jpeg"}
        )
        
        # Obtener URL pública
        public_url = supabase.storage.from_('avatars').get_public_url(filepath)
        
        print(f"✅ Imagen subida a Storage: {public_url}")
        return public_url
        
    except Exception as e:
        print(f"❌ Error subiendo a Storage: {e}")
        # Fallback: retornar data URI
        return f"data:image/jpeg;base64,{image_base64}"

def build_tryon_prompt(products_metadata):
    """Construir prompt similar a buildTryOnPrompt de Node.js"""
    
    image_descriptions = []
    instructions = []
    
    # Detectar categorías
    has_top = any(p.get('category') == 'top' for p in products_metadata)
    has_outerwear = any(p.get('category') == 'outerwear' for p in products_metadata)
    has_bottom = any(p.get('category') == 'bottom' for p in products_metadata)
    
    for idx, product in enumerate(products_metadata):
        img_num = idx + 2  # Avatar es @image 1
        name = product.get('name', 'item')
        category = product.get('category', 'top')
        
        image_descriptions.append(f"@image {img_num} is {name}")
        
        if category == 'bottom':
            instructions.append(f"Replace the blue jeans with {name} from @image {img_num}")
        elif category == 'top':
            instructions.append(f"Replace the white t-shirt with {name} from @image {img_num} (worn directly on skin, base layer)")
        elif category == 'outerwear':
            if has_top:
                instructions.append(f"Add {name} from @image {img_num} as outer jacket layer (worn OVER the shirt/blouse, clearly visible as outermost layer)")
            else:
                instructions.append(f"Add {name} from @image {img_num} as jacket (worn over the white t-shirt)")
        elif category == 'dress':
            instructions.append(f"Replace ALL clothing with {name} from @image {img_num} (full-body dress)")
        elif category == 'accessories':
            instructions.append(f"Add {name} from @image {img_num}")
        elif category == 'footwear':
            instructions.append(f"Replace footwear with {name} from @image {img_num}")
        elif category == 'head':
            instructions.append(f"Add {name} from @image {img_num} on head/face")
        else:
            instructions.append(f"Add {name} from @image {img_num}")
    
    prompt = f"""@image 1 is the base avatar (person in white t-shirt and blue jeans).
{chr(10).join(image_descriptions)}

Edit @image 1 by applying these changes:
{chr(10).join(instructions)}

CRITICAL REQUIREMENTS:
- Preserve EXACT face, skin tone, and body from @image 1
- Maintain SAME soft studio lighting as @image 1 (no harsh shadows)
- Background: solid #FFFFFF pure white (RGB 255,255,255) - same as @image 1
- Keep SAME color temperature and exposure as @image 1
- Only change the clothing/accessories as specified
- Do NOT alter face, hair, skin color, or body proportions
- Do NOT add shadows on the background

Quality: 4K, consistent lighting, photorealistic, seamless white background."""
    
    return prompt

def execute_flux_direct(job):
    """
    Ejecutar workflow de ComfyUI para try-on
    TODO: Implementar llamada real a ComfyUI API
    Por ahora, retorna mock
    """
    
    job_id = job['id']
    
    print(f"🎬 [Job {job_id}] Ejecutando workflow ComfyUI...")
    
    # 1. Descargar imágenes
    avatar_url = job['input_data']['avatar_url']
    garments = job['input_data'].get('garment_images', [])
    
    avatar_path = f"/tmp/job_{job_id}_avatar.jpg"
    download_image(avatar_url, avatar_path)
    
    garment_paths = []
    for idx, garment in enumerate(garments[:3]):  # Máximo 3 prendas
        path = f"/tmp/job_{job_id}_garment_{idx}.jpg"
        download_image(garment['url'], path)
        garment_paths.append(path)
    
    # 2. Construir prompt
    products_metadata = job['input_data'].get('products_metadata', [])
    prompt = build_tryon_prompt(products_metadata[:3])
    
    print(f"📝 [Job {job_id}] Prompt generado ({len(prompt)} chars)")
    
    # Usar ComfyUI que ya está cargado por el template
    print(f"🎬 [Job {job_id}] Generando con ComfyUI (FLUX.2)...")
    
    # ComfyUI workflow simple para FLUX.2
    workflow = {
        "3": {
            "inputs": {"text": prompt},
            "class_type": "CLIPTextEncode"
        },
        "6": {
            "inputs": {
                "text": prompt,
                "clip": ["11", 0]
            },
            "class_type": "CLIPTextEncode"
        },
        "8": {
            "inputs": {
                "samples": ["13", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEDecode"
        },
        "9": {
            "inputs": {
                "filename_prefix": f"tryon_{job_id}",
                "images": ["8", 0]
            },
            "class_type": "SaveImage"
        },
        "10": {
            "inputs": {"vae_name": "ae.safetensors"},
            "class_type": "VAELoader"
        },
        "11": {
            "inputs": {"clip_name1": "clip_l.safetensors", "clip_name2": "t5xxl_fp8_e4m3fn.safetensors"},
            "class_type": "DualCLIPLoader"
        },
        "12": {
            "inputs": {"unet_name": "flux2-dev.safetensors", "weight_dtype": "default"},
            "class_type": "UNETLoader"
        },
        "13": {
            "inputs": {
                "noise": ["25", 0],
                "guider": ["22", 0],
                "sampler": ["16", 0],
                "sigmas": ["17", 0],
                "latent_image": ["27", 0]
            },
            "class_type": "SamplerCustomAdvanced"
        },
        "16": {
            "inputs": {"sampler_name": "euler"},
            "class_type": "KSamplerSelect"
        },
        "17": {
            "inputs": {
                "scheduler": "sgm_uniform",
                "steps": 28,
                "denoise": 1.0,
                "model": ["12", 0]
            },
            "class_type": "BasicScheduler"
        },
        "22": {
            "inputs": {
                "model": ["12", 0],
                "conditioning": ["6", 0]
            },
            "class_type": "BasicGuider"
        },
        "25": {
            "inputs": {"noise_seed": int(time.time())},
            "class_type": "RandomNoise"
        },
        "27": {
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
            "class_type": "EmptyLatentImage"
        }
    }
    
    # Enviar a ComfyUI
    resp = requests.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": workflow},
        timeout=10
    )
    resp.raise_for_status()
    result = resp.json()
    prompt_id = result.get("prompt_id")
    
    if not prompt_id:
        raise Exception(f"No prompt_id en respuesta: {result}")
    
    print(f"✅ [Job {job_id}] Workflow enviado a ComfyUI, prompt_id: {prompt_id}")
    
    # Polling hasta que complete
    max_wait = 300
    start_time = time.time()
    
    while (time.time() - start_time) < max_wait:
        try:
            history_resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=5)
            history = history_resp.json()
            
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                
                if "9" in outputs and "images" in outputs["9"]:
                    images = outputs["9"]["images"]
                    if len(images) > 0:
                        filename = images[0]["filename"]
                        subfolder = images[0].get("subfolder", "")
                        
                        result_path = f"/workspace/ComfyUI/output/{subfolder}/{filename}" if subfolder else f"/workspace/ComfyUI/output/{filename}"
                        
                        if Path(result_path).exists():
                            print(f"✅ [Job {job_id}] Imagen generada: {result_path}")
                            return result_path
                
                raise Exception("Workflow completó pero sin imagen")
            
            time.sleep(3)
        except requests.exceptions.RequestException as e:
            print(f"⚠️ [Job {job_id}] Error consultando ComfyUI: {e}")
            time.sleep(3)
    
    raise Exception(f"Timeout esperando resultado ({max_wait}s)")

def upload_result_to_supabase(job_id, user_id, result_path):
    """Subir resultado a Supabase Storage"""
    
    try:
        with open(result_path, 'rb') as f:
            file_data = f.read()
        
        file_name = f"tryon_{user_id}_{job_id}_{int(time.time())}.jpg"
        storage_path = f"{user_id}/tryons/{file_name}"
        
        print(f"📤 [Job {job_id}] Subiendo a Storage ({len(file_data)/1024:.1f} KB)...")
        
        # Upload a Supabase Storage
        upload_resp = supabase.storage.from_("avatars").upload(
            storage_path,
            file_data,
            file_options={"content-type": "image/jpeg", "upsert": False}
        )
        
        print(f"📤 Upload response: {upload_resp}")
        
        # Obtener URL pública
        public_url = supabase.storage.from_("avatars").get_public_url(storage_path)
        
        # Manejar diferentes formatos de respuesta del cliente Python
        if isinstance(public_url, dict):
            public_url = public_url.get('publicUrl') or public_url.get('publicURL') or str(public_url)
        
        print(f"✅ [Job {job_id}] Subido: {public_url[:80]}...")
        
        return public_url
        
    except Exception as e:
        print(f"❌ [Job {job_id}] Error subiendo: {e}")
        raise

def process_job(job):
    """Procesar un job completo"""
    
    job_id = job['id']
    user_id = job['user_id']
    
    start_time = time.time()
    
    try:
        print(f"\n{'='*60}")
        print(f"👕 [Job {job_id}] Iniciando procesamiento")
        print(f"   User: {user_id}")
        print(f"   Type: {job.get('job_type', 'unknown')}")
        print(f"{'='*60}\n")
        
        # Actualizar estado a processing
        supabase.table('ai_generation_jobs').update({
            'status': 'processing',
            'started_at': datetime.utcnow().isoformat(),
            'progress': 10,
            'result_metadata': {
                'worker_id': WORKER_ID,
                'backend': 'vast',
                'started_at': datetime.utcnow().isoformat()
            }
        }).eq('id', job_id).execute()
        
        # Ejecutar FLUX.2 directo con diffusers
        result_path = execute_flux_direct(job)
        
        # Actualizar progreso
        supabase.table('ai_generation_jobs').update({
            'progress': 90
        }).eq('id', job_id).execute()
        
        # Subir resultado a Storage
        public_url = upload_result_to_supabase(job_id, user_id, result_path)
        
        # Guardar en tryon_results
        supabase.table('tryon_results').insert({
            'user_id': user_id,
            'job_id': job_id,
            'result_url': public_url,
            'products_used': job['input_data'].get('products_metadata', [])
        }).execute()
        
        # Marcar job como completado
        processing_time = time.time() - start_time
        
        supabase.table('ai_generation_jobs').update({
            'status': 'completed',
            'progress': 100,
            'result_url': public_url,
            'completed_at': datetime.utcnow().isoformat(),
            'processing_time_seconds': round(processing_time, 2),
            'cost_usd': 0.005,  # Costo estimado en Vast
        }).eq('id', job_id).execute()
        
        # Notificar a Vast Manager que procesamos un job
        try:
            supabase.table('vast_instances').update({
                'last_job_at': datetime.utcnow().isoformat(),
                'status': 'ready',
            }).eq('worker_id', WORKER_ID).execute()
        except Exception as e:
            print(f"⚠️ Error actualizando instancia: {e}")
        
        print(f"✅ [Job {job_id}] Completado en {processing_time:.1f}s")
        
        # Limpiar archivos temporales
        try:
            os.remove(result_path)
        except:
            pass
        
        return True
        
    except Exception as e:
        print(f"❌ [Job {job_id}] Error: {e}")
        
        # Marcar job como fallido
        supabase.table('ai_generation_jobs').update({
            'status': 'failed',
            'error_message': str(e),
            'completed_at': datetime.utcnow().isoformat(),
        }).eq('id', job_id).execute()
        
        # Incrementar contador de fallos (skip por ahora - no crítico)
        try:
            supabase.postgrest.session.execute(
                supabase.table('vast_instances')
                .update({'jobs_failed': 'jobs_failed + 1'})
                .eq('worker_id', WORKER_ID)
                .build()
            )
        except:
            pass  # No crítico si falla
        
        return False

def send_heartbeat():
    """Enviar heartbeat a Supabase"""
    try:
        supabase.table('vast_instances').update({
            'last_health_check': datetime.utcnow().isoformat(),
            'health_status': 'healthy',
        }).eq('worker_id', WORKER_ID).execute()
    except Exception as e:
        print(f"⚠️ Error enviando heartbeat: {e}")

def mark_instance_ready():
    """Marcar instancia como ready en BD"""
    try:
        supabase.table('vast_instances').update({
            'status': 'ready',
            'ready_at': datetime.utcnow().isoformat(),
            'health_status': 'healthy',
        }).eq('worker_id', WORKER_ID).execute()
        
        print(f"✅ Instancia marcada como READY en Supabase")
    except Exception as e:
        print(f"❌ Error marcando ready: {e}")

def main_loop():
    """Loop principal del worker"""
    
    print("⏳ Esperando a que ComfyUI cargue FLUX.2 en VRAM...")
    print("   Modelos ya descargados en disco, cargando en memoria GPU...")
    print("   Primera carga puede tardar 2-4 minutos (64GB → VRAM)")
    print(f"   Verificando: {COMFY_URL}/system_stats")
    
    # Esperar a que ComfyUI esté listo
    max_wait = 300  # 5 minutos (modelos ya están descargados)
    waited = 0
    
    while not check_comfy_ready() and waited < max_wait:
        time.sleep(10)
        waited += 10
        if waited % 30 == 0:
            print(f"   ⏳ Esperando... ({waited}s / {max_wait}s) - Cargando modelos en VRAM...")
    
    if not check_comfy_ready():
        print(f"❌ ComfyUI no respondió en {max_wait}s")
        print(f"   URL intentada: {COMFY_URL}")
        print("   Verificar:")
        print("   1. ComfyUI arrancó? → ps aux | grep 'python.*main.py'")
        print("   2. Puerto correcto? → netstat -tulpn | grep 8188")
        print("   3. Logs de ComfyUI → tail /workspace/comfyui.log")
        sys.exit(1)
    
    print(f"✅ ComfyUI READY en {COMFY_URL}")
    print("   Modelos cargados, listo para procesar jobs")
    
    # Marcar instancia como ready
    mark_instance_ready()
    
    # Contadores
    last_heartbeat = time.time()
    jobs_processed_total = 0
    
    print(f"\n🤖 Worker {WORKER_ID} activo y esperando jobs...\n")
    
    # Loop infinito
    while True:
        try:
            # Heartbeat periódico
            if time.time() - last_heartbeat > WORKER_CONFIG['HEARTBEAT_INTERVAL_SECONDS']:
                send_heartbeat()
                last_heartbeat = time.time()
            
            # Buscar jobs pendientes para 'vast'
            response = supabase.table('ai_generation_jobs') \
                .select('*') \
                .eq('status', 'pending') \
                .eq('preferred_backend', 'vast') \
                .order('priority', desc=True) \
                .order('created_at') \
                .limit(WORKER_CONFIG['MAX_BATCH_SIZE']) \
                .execute()
            
            jobs = response.data
            
            if not jobs or len(jobs) == 0:
                # No hay jobs - marcar como idle
                supabase.table('vast_instances').update({
                    'status': 'idle',
                    'current_batch_size': 0,
                }).eq('worker_id', WORKER_ID).execute()
                
                if jobs_processed_total % 10 == 0 and jobs_processed_total > 0:
                    print(f"💤 Sin jobs ({jobs_processed_total} procesados total)")
                
                time.sleep(WORKER_CONFIG['POLL_INTERVAL_SECONDS'])
                continue
            
            batch_size = len(jobs)
            print(f"\n🚀 Procesando batch de {batch_size} job(s)")
            
            # Marcar como busy
            supabase.table('vast_instances').update({
                'status': 'busy',
                'current_batch_size': batch_size,
            }).eq('worker_id', WORKER_ID).execute()
            
            # Procesar batch (FCFS - uno a la vez por ahora)
            # TODO: Procesar en paralelo si VRAM lo permite
            for job in jobs:
                success = process_job(job)
                if success:
                    jobs_processed_total += 1
            
            print(f"✅ Batch completado ({jobs_processed_total} total)\n")
            
        except KeyboardInterrupt:
            print("\n\n🛑 Worker detenido por usuario")
            break
            
        except Exception as e:
            print(f"❌ Error en main loop: {e}")
            time.sleep(10)  # Esperar más en caso de error
    
    print(f"\n📊 Estadísticas finales:")
    print(f"   Jobs procesados: {jobs_processed_total}")
    print(f"   Worker ID: {WORKER_ID}")
    print("\n👋 Worker finalizado")

if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        print(f"❌ Error fatal: {e}")
        sys.exit(1)

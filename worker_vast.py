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

# Script custom (NO template): puerto 8188 estándar
COMFY_URL = os.getenv("COMFYUI_API_BASE", "http://127.0.0.1:8188")

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

def execute_comfy_workflow(job):
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
    
    # 3. Crear workflow de ComfyUI para FLUX.2 Edit
    # Workflow simplificado usando text-to-image con prompt
    workflow = {
        "3": {
            "inputs": {
                "ckpt_name": "flux2_dev_fp8.safetensors"
            },
            "class_type": "CheckpointLoaderSimple",
        },
        "6": {
            "inputs": {
                "text": prompt,
                "clip": ["3", 1]
            },
            "class_type": "CLIPTextEncode",
        },
        "5": {
            "inputs": {
                "width": 1152,
                "height": 2016,
                "batch_size": 1
            },
            "class_type": "EmptyLatentImage",
        },
        "10": {
            "inputs": {
                "seed": int(time.time()),
                "steps": 28,
                "cfg": 3.5,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["3", 0],
                "positive": ["6", 0],
                "negative": ["6", 0],
                "latent_image": ["5", 0]
            },
            "class_type": "KSampler",
        },
        "8": {
            "inputs": {
                "samples": ["10", 0],
                "vae": ["3", 2]
            },
            "class_type": "VAEDecode",
        },
        "9": {
            "inputs": {
                "filename_prefix": f"tryon_{job_id}",
                "images": ["8", 0]
            },
            "class_type": "SaveImage",
        }
    }
    
    print(f"📤 [Job {job_id}] Enviando workflow a ComfyUI ({COMFY_URL})...")
    
    # Enviar workflow a ComfyUI
    try:
        resp = requests.post(
            f"{COMFY_URL}/prompt",
            json={"prompt": workflow, "client_id": WORKER_ID},
            timeout=10
        )
        resp.raise_for_status()
        result = resp.json()
        prompt_id = result.get("prompt_id")
        
        if not prompt_id:
            raise Exception(f"No prompt_id en respuesta: {result}")
        
        print(f"✅ [Job {job_id}] Workflow enviado, prompt_id: {prompt_id}")
        
        # Polling hasta que complete
        max_wait = 300  # 5 minutos máximo
        start_time = time.time()
        last_progress = 0
        
        while (time.time() - start_time) < max_wait:
            try:
                # Verificar progreso en queue
                queue_resp = requests.get(f"{COMFY_URL}/queue", timeout=5)
                queue = queue_resp.json()
                
                # Verificar si completó
                history_resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=5)
                history = history_resp.json()
                
                if prompt_id in history:
                    # Completado!
                    outputs = history[prompt_id].get("outputs", {})
                    
                    print(f"✅ [Job {job_id}] ComfyUI completó, extrayendo imagen...")
                    
                    # Buscar imagen generada (nodo 9: SaveImage)
                    if "9" in outputs and "images" in outputs["9"]:
                        images = outputs["9"]["images"]
                        if len(images) > 0:
                            filename = images[0]["filename"]
                            subfolder = images[0].get("subfolder", "")
                            img_type = images[0].get("type", "output")
                            
                            # Construir ruta según template de Vast.ai
                            # El template puede usar /root/ComfyUI/output o /workspace/ComfyUI/output
                            possible_paths = [
                                f"/root/ComfyUI/output/{subfolder}/{filename}" if subfolder else f"/root/ComfyUI/output/{filename}",
                                f"/workspace/ComfyUI/output/{subfolder}/{filename}" if subfolder else f"/workspace/ComfyUI/output/{filename}",
                            ]
                            
                            result_path = None
                            for path in possible_paths:
                                if Path(path).exists():
                                    result_path = path
                                    break
                            
                            if not result_path:
                                raise Exception(f"Imagen no encontrada en rutas esperadas: {possible_paths}")
                            
                            print(f"✅ [Job {job_id}] Imagen encontrada: {result_path}")
                            return result_path
                    
                    # Si llegó aquí, el workflow terminó pero sin imagen
                    print(f"⚠️ [Job {job_id}] Outputs disponibles: {list(outputs.keys())}")
                    raise Exception("Workflow completó pero sin imagen en nodo 9")
                
                # Aún procesando
                elapsed = int(time.time() - start_time)
                if elapsed > last_progress + 10:
                    print(f"⏳ [Job {job_id}] Procesando... ({elapsed}s)")
                    last_progress = elapsed
                
            except requests.exceptions.RequestException as e:
                print(f"⚠️ [Job {job_id}] Error consultando ComfyUI: {e}")
            
            time.sleep(3)
        
        raise Exception(f"Timeout esperando resultado de ComfyUI ({max_wait}s)")
        
    except Exception as e:
        print(f"❌ [Job {job_id}] Error en ComfyUI: {e}")
        raise

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
        
        # Ejecutar ComfyUI workflow
        result_path = execute_comfy_workflow(job)
        
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
        supabase.table('vast_instances').update({
            'last_job_at': datetime.utcnow().isoformat(),
            'jobs_processed': supabase.rpc('increment', {'x': 1}),
            'status': 'ready',  # Volver a ready después de procesar
        }).eq('worker_id', WORKER_ID).execute()
        
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
        
        # Incrementar contador de fallos
        supabase.table('vast_instances').update({
            'jobs_failed': supabase.rpc('increment', {'x': 1}),
        }).eq('worker_id', WORKER_ID).execute()
        
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
    
    print("⏳ Esperando ComfyUI y provisioning script del template...")
    print("   El template está descargando FLUX.2 (~15GB), puede tardar 10-15 minutos...")
    print(f"   Verificando en: {COMFY_URL}")
    
    # Esperar a que ComfyUI esté listo (template tarda en descargar modelos)
    max_wait = 900  # 15 minutos para primera vez
    waited = 0
    
    while not check_comfy_ready() and waited < max_wait:
        time.sleep(10)
        waited += 10
        if waited % 60 == 0:
            elapsed_min = waited / 60
            print(f"   ⏳ Esperando... ({elapsed_min:.0f} min / {max_wait/60:.0f} min) - Descargando FLUX.2 y modelos...")
    
    if not check_comfy_ready():
        print(f"❌ ComfyUI no respondió en {max_wait/60} minutos")
        print("   Posibles causas:")
        print("   1. Provisioning script aún descargando (revisar logs de Vast.ai)")
        print("   2. ComfyUI falló al arrancar")
        print(f"   3. Puerto incorrecto (esperando en {COMFY_URL})")
        print("   Tip: Revisa logs en https://cloud.vast.ai/instances/")
        sys.exit(1)
    
    print(f"✅ ComfyUI READY en {COMFY_URL}")
    print("   FLUX.2 descargado y listo para procesar jobs")
    
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

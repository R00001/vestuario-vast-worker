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

def build_tryon_prompt_comfyui(products_metadata):
    """
    Construir prompt para ComfyUI (sin formato @image)
    ComfyUI usa LoadImage + ReferenceLatent, no texto con @image
    """
    
    if not products_metadata:
        return "A person wearing casual clothing, professional studio photo, white background"
    
    # Describir las prendas que se aplicarán
    items = []
    for p in products_metadata:
        name = p.get('name', 'clothing item')
        category = p.get('category', 'clothing')
        items.append(f"{name} ({category})")
    
    items_desc = ", ".join(items)
    
    prompt = f"""Full body portrait photo of a person wearing: {items_desc}.
    
The person has natural appearance, standing straight, facing camera directly.
Professional studio photography, soft even lighting.
Background: pure white seamless (#FFFFFF).
High quality, 4K, sharp focus, photorealistic.
Clothing fits naturally on the body with realistic folds and shadows.
Preserve natural skin tone and facial features."""
    
    return prompt


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

def execute_face_enhancement(job):
    """
    Generar foto HD de rostro frontal con fondo blanco
    Usa FLUX.2 para mejorar/generar cara del usuario
    """
    
    job_id = job['id']
    
    print(f"🎭 [Job {job_id}] Ejecutando face enhancement...")
    
    # Directorio input de ComfyUI
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    Path(COMFY_INPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    # Descargar foto de cara del usuario
    face_url = job['input_data']['face_photo_url']
    face_filename = f"face_{job_id}.jpg"
    face_path = f"{COMFY_INPUT_DIR}/{face_filename}"
    
    print(f"📥 [Job {job_id}] Descargando foto de cara...")
    download_image(face_url, face_path)
    
    # Obtener datos del análisis facial si están disponibles
    facial_analysis = job['input_data'].get('facial_analysis', {})
    gender = job['input_data'].get('gender', 'person')
    
    # Construir prompt para generar HD face
    gender_term = "man" if gender == "male" else "woman" if gender == "female" else "person"
    
    # Características faciales del análisis Grok
    face_shape = facial_analysis.get('face_shape', '')
    skin_tone = facial_analysis.get('skin_tone', {})
    eyes = facial_analysis.get('eyes', {})
    hair = facial_analysis.get('hair', {})
    
    face_desc_parts = []
    if face_shape:
        face_desc_parts.append(f"{face_shape} face shape")
    if eyes.get('color'):
        face_desc_parts.append(f"{eyes['color']} eyes")
    if hair.get('color') and hair.get('type'):
        face_desc_parts.append(f"{hair['color']} {hair['type']} hair")
    
    face_desc = ", ".join(face_desc_parts) if face_desc_parts else ""
    
    prompt = f"""Professional headshot portrait of the same {gender_term} from the reference image.
{f"Features: {face_desc}." if face_desc else ""}

CRITICAL: Preserve EXACT facial features, skin tone, eye shape, nose, lips, face shape from reference.
Direct frontal view, eyes looking straight at camera.
Neutral relaxed expression.
Background: pure white seamless (#FFFFFF).
Soft even studio lighting, no harsh shadows.
High definition, 4K, sharp focus, natural skin texture.
Professional ID photo quality."""

    seed = int(time.time()) % 999999999
    
    # Workflow para face enhancement
    workflow = {
        # === MODELOS ===
        "12": {
            "inputs": {
                "unet_name": "flux2_dev_fp8mixed.safetensors",
                "weight_dtype": "default"
            },
            "class_type": "UNETLoader"
        },
        "38": {
            "inputs": {
                "clip_name": "mistral_3_small_flux2_bf16.safetensors",
                "type": "flux2",
                "device": "default"
            },
            "class_type": "CLIPLoader"
        },
        "10": {
            "inputs": {
                "vae_name": "flux2-vae.safetensors"
            },
            "class_type": "VAELoader"
        },
        
        # === PROMPT ===
        "6": {
            "inputs": {
                "text": prompt,
                "clip": ["38", 0]
            },
            "class_type": "CLIPTextEncode"
        },
        "26": {
            "inputs": {
                "guidance": 4.0,
                "conditioning": ["6", 0]
            },
            "class_type": "FluxGuidance"
        },
        
        # === CARGAR IMAGEN REFERENCIA ===
        "42": {
            "inputs": {
                "image": face_filename
            },
            "class_type": "LoadImage"
        },
        "41": {
            "inputs": {
                "upscale_method": "area",
                "megapixels": 1.0,
                "sharpen": 1,
                "resolution_steps": 64,
                "image": ["42", 0]
            },
            "class_type": "ImageScaleToTotalPixels"
        },
        
        # === VAE ENCODE (img2img) ===
        "40": {
            "inputs": {
                "pixels": ["41", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        },
        
        # === REFERENCIA FACIAL ===
        "50": {
            "inputs": {
                "method": "index",
                "reference_latent": ["40", 0]
            },
            "class_type": "ReferenceLatent"
        },
        
        # === SAMPLER ===
        "13": {
            "inputs": {
                "noise": ["25", 0],
                "guider": ["22", 0],
                "sampler": ["16", 0],
                "sigmas": ["48", 0],
                "latent_image": ["40", 0]
            },
            "class_type": "SamplerCustomAdvanced"
        },
        "25": {
            "inputs": {
                "noise_seed": seed
            },
            "class_type": "RandomNoise"
        },
        "16": {
            "inputs": {
                "sampler_name": "euler"
            },
            "class_type": "KSamplerSelect"
        },
        "48": {
            "inputs": {
                "steps": 20,
                "denoise": 0.55,  # Menor denoise para preservar identidad
                "width": 1024,
                "height": 1024  # Cuadrado para cara
            },
            "class_type": "Flux2Scheduler"
        },
        "22": {
            "inputs": {
                "model": ["12", 0],
                "conditioning": ["26", 0],
                "reference_latent": ["50", 0]
            },
            "class_type": "BasicGuider"
        },
        
        # === DECODE Y GUARDAR ===
        "8": {
            "inputs": {
                "samples": ["13", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEDecode"
        },
        "9": {
            "inputs": {
                "filename_prefix": f"face_{job_id}",
                "images": ["8", 0]
            },
            "class_type": "SaveImage"
        }
    }
    
    # Enviar a ComfyUI
    payload = {"prompt": workflow}
    
    resp = requests.post(f"{COMFY_URL}/prompt", json=payload, timeout=60)
    resp.raise_for_status()
    
    prompt_id = resp.json()['prompt_id']
    print(f"📤 [Job {job_id}] ComfyUI prompt_id: {prompt_id}")
    
    # Esperar resultado
    max_wait = 120  # 2 minutos para face
    waited = 0
    
    while waited < max_wait:
        time.sleep(3)
        waited += 3
        
        hist_resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
        history = hist_resp.json()
        
        if prompt_id in history:
            outputs = history[prompt_id].get('outputs', {})
            
            if '9' in outputs and outputs['9'].get('images'):
                image_info = outputs['9']['images'][0]
                result_filename = image_info['filename']
                result_subfolder = image_info.get('subfolder', '')
                
                result_path = f"/workspace/ComfyUI/output/{result_subfolder}/{result_filename}" if result_subfolder else f"/workspace/ComfyUI/output/{result_filename}"
                
                print(f"✅ [Job {job_id}] Face enhancement completado: {result_path}")
                return result_path
            
            if history[prompt_id].get('status', {}).get('completed', False):
                raise Exception("ComfyUI completó pero no hay output")
    
    raise Exception(f"Face enhancement timeout ({max_wait}s)")


def execute_avatar_generation(job):
    """
    Generar avatar base 2K (cuerpo completo) del usuario
    Combina foto de cara + análisis corporal
    """
    
    job_id = job['id']
    
    print(f"🎭 [Job {job_id}] Generando avatar base...")
    
    # Directorio input de ComfyUI
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    Path(COMFY_INPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    # Descargar foto HD de cara (ya generada por face_enhancement)
    face_url = job['input_data']['face_hd_url']
    face_filename = f"face_hd_{job_id}.jpg"
    face_path = f"{COMFY_INPUT_DIR}/{face_filename}"
    
    print(f"📥 [Job {job_id}] Descargando face HD...")
    download_image(face_url, face_path)
    
    # Datos del usuario
    gender = job['input_data'].get('gender', 'person')
    body_analysis = job['input_data'].get('body_analysis', {})
    height_cm = job['input_data'].get('height_cm', 170)
    
    gender_term = "man" if gender == "male" else "woman" if gender == "female" else "person"
    
    # Características corporales
    body_type = body_analysis.get('body_type', {}).get('primary', 'average')
    proportions = body_analysis.get('proportions', {})
    
    body_desc_parts = []
    if body_type:
        body_desc_parts.append(f"{body_type} build")
    if proportions.get('shoulder_width'):
        body_desc_parts.append(f"{proportions['shoulder_width']} shoulders")
    
    body_desc = ", ".join(body_desc_parts) if body_desc_parts else ""
    
    prompt = f"""Full body portrait of the same {gender_term} from the reference face image.
{f"Body: {body_desc}." if body_desc else ""}
Height approximately {height_cm}cm.

Wearing plain white t-shirt and blue jeans. White sneakers.
Standing straight, arms relaxed at sides, facing camera directly.
CRITICAL: Face MUST match reference image exactly - same facial features, skin tone, expression.
Background: pure white seamless (#FFFFFF).
Professional studio photography, soft even lighting.
Full body visible from head to feet.
High definition, 4K, photorealistic, natural proportions."""

    seed = int(time.time()) % 999999999
    
    # Workflow para avatar base - similar a face pero 9:16
    workflow = {
        # === MODELOS ===
        "12": {
            "inputs": {
                "unet_name": "flux2_dev_fp8mixed.safetensors",
                "weight_dtype": "default"
            },
            "class_type": "UNETLoader"
        },
        "38": {
            "inputs": {
                "clip_name": "mistral_3_small_flux2_bf16.safetensors",
                "type": "flux2",
                "device": "default"
            },
            "class_type": "CLIPLoader"
        },
        "10": {
            "inputs": {
                "vae_name": "flux2-vae.safetensors"
            },
            "class_type": "VAELoader"
        },
        
        # === PROMPT ===
        "6": {
            "inputs": {
                "text": prompt,
                "clip": ["38", 0]
            },
            "class_type": "CLIPTextEncode"
        },
        "26": {
            "inputs": {
                "guidance": 4.5,
                "conditioning": ["6", 0]
            },
            "class_type": "FluxGuidance"
        },
        
        # === CARGAR FACE HD COMO REFERENCIA ===
        "42": {
            "inputs": {
                "image": face_filename
            },
            "class_type": "LoadImage"
        },
        "41": {
            "inputs": {
                "upscale_method": "area",
                "megapixels": 1.5,
                "sharpen": 1,
                "resolution_steps": 64,
                "image": ["42", 0]
            },
            "class_type": "ImageScaleToTotalPixels"
        },
        
        # === VAE ENCODE ===
        "40": {
            "inputs": {
                "pixels": ["41", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        },
        
        # === REFERENCIA FACIAL ===
        "50": {
            "inputs": {
                "method": "index",
                "reference_latent": ["40", 0]
            },
            "class_type": "ReferenceLatent"
        },
        
        # === LATENT VACÍO 9:16 ===
        "47": {
            "inputs": {
                "width": 1024,
                "height": 1536,
                "batch_size": 1
            },
            "class_type": "EmptyLatentImage"
        },
        
        # === SAMPLER (text2img con referencia facial) ===
        "13": {
            "inputs": {
                "noise": ["25", 0],
                "guider": ["22", 0],
                "sampler": ["16", 0],
                "sigmas": ["48", 0],
                "latent_image": ["47", 0]  # Latent vacío, no img2img
            },
            "class_type": "SamplerCustomAdvanced"
        },
        "25": {
            "inputs": {
                "noise_seed": seed
            },
            "class_type": "RandomNoise"
        },
        "16": {
            "inputs": {
                "sampler_name": "euler"
            },
            "class_type": "KSamplerSelect"
        },
        "48": {
            "inputs": {
                "steps": 25,
                "denoise": 1.0,  # Full generation desde latent vacío
                "width": 1024,
                "height": 1536
            },
            "class_type": "Flux2Scheduler"
        },
        "22": {
            "inputs": {
                "model": ["12", 0],
                "conditioning": ["26", 0],
                "reference_latent": ["50", 0]
            },
            "class_type": "BasicGuider"
        },
        
        # === DECODE Y GUARDAR ===
        "8": {
            "inputs": {
                "samples": ["13", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEDecode"
        },
        "9": {
            "inputs": {
                "filename_prefix": f"avatar_{job_id}",
                "images": ["8", 0]
            },
            "class_type": "SaveImage"
        }
    }
    
    # Enviar a ComfyUI
    payload = {"prompt": workflow}
    
    resp = requests.post(f"{COMFY_URL}/prompt", json=payload, timeout=60)
    resp.raise_for_status()
    
    prompt_id = resp.json()['prompt_id']
    print(f"📤 [Job {job_id}] ComfyUI prompt_id: {prompt_id}")
    
    # Esperar resultado
    max_wait = 180  # 3 minutos para avatar completo
    waited = 0
    
    while waited < max_wait:
        time.sleep(3)
        waited += 3
        
        hist_resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
        history = hist_resp.json()
        
        if prompt_id in history:
            outputs = history[prompt_id].get('outputs', {})
            
            if '9' in outputs and outputs['9'].get('images'):
                image_info = outputs['9']['images'][0]
                result_filename = image_info['filename']
                result_subfolder = image_info.get('subfolder', '')
                
                result_path = f"/workspace/ComfyUI/output/{result_subfolder}/{result_filename}" if result_subfolder else f"/workspace/ComfyUI/output/{result_filename}"
                
                print(f"✅ [Job {job_id}] Avatar base generado: {result_path}")
                return result_path
            
            if history[prompt_id].get('status', {}).get('completed', False):
                raise Exception("ComfyUI completó pero no hay output")
    
    raise Exception(f"Avatar generation timeout ({max_wait}s)")


def execute_flux_direct(job):
    """
    Ejecutar workflow de ComfyUI para try-on
    Usa el workflow JSON con imágenes de referencia
    """
    
    job_id = job['id']
    
    print(f"🎬 [Job {job_id}] Ejecutando workflow ComfyUI...")
    
    # Directorio input de ComfyUI
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    Path(COMFY_INPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    # 1. Descargar avatar al directorio input de ComfyUI
    avatar_url = job['input_data']['avatar_url']
    avatar_filename = f"avatar_{job_id}.jpg"
    avatar_path = f"{COMFY_INPUT_DIR}/{avatar_filename}"
    
    print(f"📥 [Job {job_id}] Descargando avatar...")
    download_image(avatar_url, avatar_path)
    print(f"   Guardado: {avatar_path}")
    
    # 2. Descargar prendas
    garments = job['input_data'].get('garment_images', [])
    garment_filenames = []
    
    for idx, garment in enumerate(garments[:3]):  # Máximo 3 prendas
        filename = f"garment_{job_id}_{idx}.jpg"
        path = f"{COMFY_INPUT_DIR}/{filename}"
        print(f"📥 [Job {job_id}] Descargando prenda {idx + 1}...")
        download_image(garment['url'], path)
        garment_filenames.append(filename)
    
    # 3. Construir prompt (formato ComfyUI, sin @image)
    products_metadata = job['input_data'].get('products_metadata', [])
    prompt = build_tryon_prompt_comfyui(products_metadata[:3])
    
    print(f"📝 [Job {job_id}] Prompt generado ({len(prompt)} chars)")
    
    # Usar ComfyUI que ya está cargado por el template
    print(f"🎬 [Job {job_id}] Generando con ComfyUI (FLUX.2)...")
    
    # ComfyUI workflow para FLUX.2 Try-On
    # Usa avatar + prendas como referencias encadenadas
    seed = int(time.time()) % 999999999
    
    # Construir workflow dinámicamente basado en número de prendas
    workflow = {
        # === MODELOS ===
        "12": {
            "inputs": {
                "unet_name": "flux2_dev_fp8mixed.safetensors",
                "weight_dtype": "default"
            },
            "class_type": "UNETLoader"
        },
        "38": {
            "inputs": {
                "clip_name": "mistral_3_small_flux2_bf16.safetensors",
                "type": "flux2",
                "device": "default"
            },
            "class_type": "CLIPLoader"
        },
        "10": {
            "inputs": {
                "vae_name": "flux2-vae.safetensors"
            },
            "class_type": "VAELoader"
        },
        
        # === PROMPT ===
        "6": {
            "inputs": {
                "text": prompt,
                "clip": ["38", 0]
            },
            "class_type": "CLIPTextEncode"
        },
        "26": {
            "inputs": {
                "guidance": 4.0,
                "conditioning": ["6", 0]
            },
            "class_type": "FluxGuidance"
        },
        
        # === AVATAR: LoadImage → Scale → VAEEncode ===
        "42": {
            "inputs": {
                "image": avatar_filename,
                "upload": "image"
            },
            "class_type": "LoadImage"
        },
        "41": {
            "inputs": {
                "upscale_method": "area",
                "megapixels": 1.0,
                "sharpen": 1,
                "resolution_steps": 64,
                "image": ["42", 0]
            },
            "class_type": "ImageScaleToTotalPixels"
        },
        "40": {
            "inputs": {
                "pixels": ["41", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        },
        
        # === REFERENCE 1: Avatar (persona base) ===
        "39": {
            "inputs": {
                "conditioning": ["26", 0],
                "latent": ["40", 0]
            },
            "class_type": "ReferenceLatent"
        },
        
        # === NOISE, SAMPLER, SCHEDULER ===
        "25": {
            "inputs": {
                "noise_seed": seed
            },
            "class_type": "RandomNoise"
        },
        "16": {
            "inputs": {
                "sampler_name": "euler"
            },
            "class_type": "KSamplerSelect"
        },
        "48": {
            "inputs": {
                "steps": 20,
                "denoise": 0.75,
                "width": 1024,
                "height": 1536  # 9:16 ratio para try-on full body
            },
            "class_type": "Flux2Scheduler"
        },
        # NOTA: Ya no usamos EmptyFlux2LatentImage
        # El latente del avatar (nodo 40) se usa directamente en el sampler
    }
    
    # === AÑADIR PRENDAS COMO REFERENCIAS ENCADENADAS ===
    # Cada prenda: LoadImage → Scale → VAEEncode → ReferenceLatent
    last_conditioning_node = "39"  # Avatar es la primera referencia
    
    for idx, garment_file in enumerate(garment_filenames):
        load_id = f"g{idx}_load"      # ej: g0_load
        scale_id = f"g{idx}_scale"    # ej: g0_scale
        encode_id = f"g{idx}_encode"  # ej: g0_encode
        ref_id = f"g{idx}_ref"        # ej: g0_ref
        
        # LoadImage para prenda
        workflow[load_id] = {
            "inputs": {
                "image": garment_file,
                "upload": "image"
            },
            "class_type": "LoadImage"
        }
        
        # Escalar prenda
        workflow[scale_id] = {
            "inputs": {
                "upscale_method": "area",
                "megapixels": 1.0,
                "sharpen": 1,
                "resolution_steps": 64,
                "image": [load_id, 0]
            },
            "class_type": "ImageScaleToTotalPixels"
        }
        
        # VAEEncode prenda
        workflow[encode_id] = {
            "inputs": {
                "pixels": [scale_id, 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        }
        
        # ReferenceLatent encadenado (toma conditioning del anterior)
        workflow[ref_id] = {
            "inputs": {
                "conditioning": [last_conditioning_node, 0],
                "latent": [encode_id, 0]
            },
            "class_type": "ReferenceLatent"
        }
        
        last_conditioning_node = ref_id
        print(f"   📎 Prenda {idx + 1} añadida: {garment_file} → {ref_id}")
    
    # === GUIDER: Usa el último conditioning (con todas las referencias) ===
    workflow["22"] = {
        "inputs": {
            "model": ["12", 0],
            "conditioning": [last_conditioning_node, 0]
        },
        "class_type": "BasicGuider"
    }
    
    # === SAMPLER CUSTOM ADVANCED (img2img - usa latente del avatar) ===
    workflow["13"] = {
        "inputs": {
            "noise": ["25", 0],
            "guider": ["22", 0],
            "sampler": ["16", 0],
            "sigmas": ["48", 0],
            "latent_image": ["40", 0]  # ← AVATAR latente (img2img, no vacío)
        },
        "class_type": "SamplerCustomAdvanced"
    }
    
    # === VAE DECODE ===
    workflow["8"] = {
        "inputs": {
            "samples": ["13", 0],
            "vae": ["10", 0]
        },
        "class_type": "VAEDecode"
    }
    
    # === SAVE IMAGE ===
    workflow["9"] = {
        "inputs": {
            "filename_prefix": f"tryon_{job_id}",
            "images": ["8", 0]
        },
        "class_type": "SaveImage"
    }
    
    # Enviar a ComfyUI (formato correcto según docs)
    payload = {
        "prompt": workflow,
        "client_id": WORKER_ID
    }
    
    print(f"📤 [Job {job_id}] Enviando payload a ComfyUI...")
    print(f"   URL: {COMFY_URL}/prompt")
    
    resp = requests.post(
        f"{COMFY_URL}/prompt",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    print(f"📥 [Job {job_id}] Respuesta HTTP: {resp.status_code}")
    
    if resp.status_code != 200:
        print(f"❌ [Job {job_id}] Error HTTP {resp.status_code}")
        print(f"   Response: {resp.text[:500]}")
        raise Exception(f"ComfyUI returned {resp.status_code}: {resp.text[:200]}")
    
    result = resp.json()
    print(f"📋 [Job {job_id}] Respuesta JSON: {json.dumps(result, indent=2)[:500]}")
    
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
        
        # Ejecutar según tipo de job
        job_type = job.get('job_type', 'tryon')
        
        if job_type == 'face_enhancement':
            result_path = execute_face_enhancement(job)
        elif job_type == 'avatar_generation':
            result_path = execute_avatar_generation(job)
        else:
            # Default: try-on
            result_path = execute_flux_direct(job)
        
        # Actualizar progreso
        supabase.table('ai_generation_jobs').update({
            'progress': 90
        }).eq('id', job_id).execute()
        
        # Subir resultado a Storage
        public_url = upload_result_to_supabase(job_id, user_id, result_path)
        
        # Guardar resultado según tipo de job
        if job_type == 'face_enhancement':
            # Actualizar profiles con la cara HD
            supabase.table('profiles').update({
                'face_hd_url': public_url,
                'face_enhanced_at': datetime.utcnow().isoformat()
            }).eq('id', user_id).execute()
        elif job_type == 'avatar_generation':
            # Actualizar profiles con el avatar base
            supabase.table('profiles').update({
                'avatar_url': public_url,
                'avatar_generated_at': datetime.utcnow().isoformat()
            }).eq('id', user_id).execute()
        else:
            # Try-on: guardar en tryon_results
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
    print("   Template descarga modelo (~12 min) + carga en VRAM (~5 min)")
    print("   Primera vez puede tardar hasta 20 minutos total")
    print(f"   Verificando: {COMFY_URL}/system_stats")
    
    # Esperar a que ComfyUI esté listo
    max_wait = 600  # 10 minutos (provision-looks.sh ya esperó antes)
    waited = 0
    
    while not check_comfy_ready() and waited < max_wait:
        time.sleep(10)
        waited += 10
        if waited % 60 == 0:
            print(f"   ⏳ Esperando... ({waited//60} min / {max_wait//60} min) - Cargando modelos en VRAM...")
    
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

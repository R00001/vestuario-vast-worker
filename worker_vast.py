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
from PIL import Image

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

# ============================================
# CONFIGURACIÓN DE MODELO (auto-detect: NVFP4 > bf16 > fp8)
# ============================================
# weight_dtype válidos: 'default', 'fp8_e4m3fn', 'fp8_e4m3fn_fast', 'fp8_e5m2'
# Para bf16/nvfp4 usar 'default' - ComfyUI detecta automáticamente

def get_optimal_unet_config():
    """Detecta el mejor modelo disponible (prioriza NVFP4 puro = máxima velocidad)"""
    models_dir = "/workspace/ComfyUI/models/diffusion_models"
    
    # Listar modelos disponibles
    print(f"\n🔍 Buscando modelos en: {models_dir}")
    if os.path.exists(models_dir):
        models = [f for f in os.listdir(models_dir) if f.endswith('.safetensors')]
        print(f"   Modelos encontrados: {models}")
    else:
        print(f"   ⚠️ Directorio no existe!")
        models = []
    
    # Prioridad: NVFP4 puro > NVFP4 mixed > fp8
    if os.path.exists(f"{models_dir}/flux2-dev-nvfp4.safetensors"):
        print("   ⚡ Seleccionado: NVFP4 puro (máxima velocidad)")
        return {"name": "flux2-dev-nvfp4.safetensors", "dtype": "default"}
    elif os.path.exists(f"{models_dir}/flux2-dev-nvfp4-mixed.safetensors"):
        print("   ⚡ Seleccionado: NVFP4-mixed")
        return {"name": "flux2-dev-nvfp4-mixed.safetensors", "dtype": "default"}
    elif os.path.exists(f"{models_dir}/flux2_dev_fp8mixed.safetensors"):
        print("   📦 Seleccionado: fp8 (del template)")
        return {"name": "flux2_dev_fp8mixed.safetensors", "dtype": "fp8_e4m3fn_fast"}
    else:
        print("   ⚠️ Ningún modelo encontrado, usando default")
        return {"name": "flux2_dev_fp8mixed.safetensors", "dtype": "default"}

# Se inicializa al arrancar
UNET_CONFIG = {"name": "flux2_dev_fp8mixed.safetensors", "dtype": "default"}

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

def update_job_progress(job_id, progress, message=None):
    """Actualizar progreso del job en Supabase (para Realtime)"""
    try:
        update_data = {
            'progress': min(progress, 99),  # No llegar a 100 hasta completar
            'updated_at': datetime.utcnow().isoformat()
        }
        if message:
            update_data['result_metadata'] = {'status_message': message}
        
        supabase.table('ai_generation_jobs').update(update_data).eq('id', job_id).execute()
        print(f"📊 [Job {job_id}] Progreso: {progress}% {f'- {message}' if message else ''}")
    except Exception as e:
        print(f"⚠️ Error actualizando progreso: {e}")

def get_comfy_queue_progress(prompt_id):
    """Obtener progreso de ComfyUI via /queue endpoint"""
    try:
        resp = requests.get(f"{COMFY_URL}/queue", timeout=5)
        if resp.status_code == 200:
            queue = resp.json()
            # Verificar si nuestro prompt está ejecutándose
            running = queue.get('queue_running', [])
            for item in running:
                if item[1] == prompt_id:
                    # Item encontrado, está ejecutándose
                    return {'status': 'running', 'position': 0}
            
            pending = queue.get('queue_pending', [])
            for i, item in enumerate(pending):
                if item[1] == prompt_id:
                    return {'status': 'pending', 'position': i + 1}
            
            # No está en cola, probablemente terminó
            return {'status': 'completed', 'position': 0}
    except:
        pass
    return {'status': 'unknown', 'position': 0}

def wait_for_comfy_result(job_id, prompt_id, output_node_id, max_wait=180, total_steps=20):
    """
    Esperar resultado de ComfyUI con actualizaciones de progreso REAL
    Consulta /progress para obtener el step actual
    """
    waited = 0
    last_progress = 20  # Empezamos en 20% (ya enviado antes de llamar)
    
    while waited < max_wait:
        time.sleep(2)  # Polling cada 2 segundos
        waited += 2
        
        # Obtener progreso REAL de ComfyUI
        try:
            # El endpoint /progress da info del job activo
            progress_resp = requests.get(f"{COMFY_URL}/progress", timeout=5)
            if progress_resp.status_code == 200:
                progress_data = progress_resp.json()
                # progress_data = {"value": current_step, "max": total_steps}
                current_step = progress_data.get('value', 0)
                max_steps = progress_data.get('max', total_steps)
                
                if max_steps > 0 and current_step > 0:
                    # Mapear steps a progreso: 20% (inicio) a 85% (fin generación)
                    # 65% del rango para los steps
                    real_progress = 20 + int((current_step / max_steps) * 65)
                    
                    if real_progress > last_progress:
                        update_job_progress(job_id, real_progress, f"Step {current_step}/{max_steps}")
                        last_progress = real_progress
        except:
            pass  # Si falla /progress, seguimos con history
        
        # Verificar si ComfyUI terminó
        try:
            hist_resp = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10)
            history = hist_resp.json()
            
            if prompt_id in history:
                outputs = history[prompt_id].get('outputs', {})
                
                if output_node_id in outputs and outputs[output_node_id].get('images'):
                    image_info = outputs[output_node_id]['images'][0]
                    result_filename = image_info['filename']
                    result_subfolder = image_info.get('subfolder', '')
                    
                    result_path = f"/workspace/ComfyUI/output/{result_subfolder}/{result_filename}" if result_subfolder else f"/workspace/ComfyUI/output/{result_filename}"
                    
                    update_job_progress(job_id, 90, "Subiendo resultado...")
                    return result_path
                
                # Verificar errores
                status = history[prompt_id].get('status', {})
                if status.get('status_str') == 'error':
                    error_msg = status.get('messages', [['', 'Error desconocido']])[0][1]
                    raise Exception(f"ComfyUI error: {error_msg}")
                
                if status.get('completed', False):
                    raise Exception("ComfyUI completó pero no hay output en el nodo esperado")
        except requests.exceptions.RequestException as e:
            print(f"⚠️ Error consultando history: {e}")
    
    raise Exception(f"Timeout esperando resultado ({max_wait}s)")

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


def concatenate_images_for_flux(image_paths, output_path, target_height=1024):
    """
    Concatenar imágenes horizontalmente para FLUX.2 Edit
    El prompt usará @image 1, @image 2, etc. para referenciar cada sección
    """
    if not image_paths:
        raise ValueError("No hay imágenes para concatenar")
    
    images = []
    for path in image_paths:
        img = Image.open(path).convert('RGB')
        # Redimensionar manteniendo aspect ratio
        ratio = target_height / img.height
        new_width = int(img.width * ratio)
        resized = img.resize((new_width, target_height), Image.Resampling.LANCZOS)
        images.append(resized)
    
    # Calcular ancho total
    total_width = sum(img.width for img in images)
    
    # Crear imagen concatenada
    concat_img = Image.new('RGB', (total_width, target_height))
    
    # Pegar cada imagen
    x_offset = 0
    for img in images:
        concat_img.paste(img, (x_offset, 0))
        x_offset += img.width
    
    # Guardar
    concat_img.save(output_path, 'JPEG', quality=95)
    print(f"   📎 Concatenado: {len(image_paths)} imágenes → {total_width}x{target_height}px")
    
    return output_path


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

def build_tryon_prompt_comfyui(products_metadata, settings=None, avatar_info=None):
    """
    ============================================
    PROMPT BUILDER - Try-On Profesional (ComfyUI)
    ============================================
    
    Genera prompts estructurados para FLUX.2 via ComfyUI
    Soporta: Avatar + N productos (1-5)
    
    Estructura JSON interna:
    {
        "subject": { "model_description", "base_clothing" },
        "garments": [{ "index", "name", "category", "instruction" }],
        "scene": { "background", "color_accent", "pose", "lighting" },
        "technical": { "quality", "format" }
    }
    
    Args:
        products_metadata: Lista de productos (máx 5)
        settings: Config de background, pose, lighting, colors
        avatar_info: Grok analysis del avatar
    
    Returns:
        str: Prompt renderizado para ComfyUI
    """
    
    if not products_metadata:
        return "A person wearing casual clothing, professional studio photo, white background"
    
    # ============================================
    # 1. CONSTRUIR ESTRUCTURA JSON
    # ============================================
    prompt_data = {
        "subject": {
            "base_image": "@image 1",
            "base_clothing": "white t-shirt and blue jeans",
            "model": build_model_description(avatar_info),
        },
        "garments": [],
        "scene": {
            "background": "pure white seamless studio",
            "color_accent": None,
            "pose": "standing straight, facing camera, arms relaxed at sides",
            "lighting": "soft professional studio lighting, even illumination",
        },
        "technical": {
            "quality": "4K, sharp focus, photorealistic",
            "style": "fashion photography, commercial",
        }
    }
    
    # Contexto para instrucciones
    has_top = any(p.get('category') == 'top' for p in products_metadata)
    has_dress = any(p.get('category') in ['dress', 'set'] for p in products_metadata)
    has_outerwear = any(p.get('category') == 'outerwear' for p in products_metadata)
    
    # Procesar cada producto (Avatar es @image 1, productos son @image 2-6)
    for idx, product in enumerate(products_metadata):
        image_index = idx + 2  # @image 2, 3, 4, 5, 6
        garment = {
            "image_ref": f"@image {image_index}",
            "name": product.get('name', 'clothing item'),
            "category": product.get('category', 'other'),
            "instruction": build_garment_instruction(product, image_index, has_top, has_dress),
        }
        prompt_data["garments"].append(garment)
    
    # ============================================
    # 2. PROCESAR SCENE SETTINGS
    # ============================================
    if settings:
        bg = settings.get('background', {})
        bg_color = settings.get('backgroundColor')
        
        # Background
        if bg.get('type') == 'color' and bg_color:
            prompt_data["scene"]["background"] = f"solid {bg_color} color, clean seamless"
            prompt_data["scene"]["color_accent"] = bg_color
        elif bg.get('type') == 'preset' and bg.get('prompt_addon'):
            prompt_data["scene"]["background"] = bg.get('prompt_addon')
            if bg_color and bg_color != '#FFFFFF':
                prompt_data["scene"]["color_accent"] = bg_color
        elif bg.get('type') == 'auto':
            prompt_data["scene"]["background"] = determine_auto_background(products_metadata)
        
        # Pose
        if settings.get('pose', {}).get('prompt_addon'):
            prompt_data["scene"]["pose"] = settings['pose']['prompt_addon']
        
        # Lighting
        if settings.get('lighting', {}).get('prompt_addon'):
            prompt_data["scene"]["lighting"] = settings['lighting']['prompt_addon']
    
    # Log estructura JSON
    print(f"📋 Prompt Structure: {json.dumps(prompt_data, indent=2, default=str)}")
    
    # ============================================
    # 3. RENDERIZAR A TEXTO
    # ============================================
    return render_tryon_prompt(prompt_data)


def build_model_description(avatar_info):
    """Construir descripción del modelo desde Grok analysis"""
    if not avatar_info:
        return "person"
    
    facial = avatar_info.get('grok_facial_features', avatar_info.get('facial', {}))
    body = avatar_info.get('grok_body_analysis', avatar_info.get('body', {}))
    
    traits = []
    
    if facial.get('gender_presentation'):
        traits.append(facial['gender_presentation'])
    if facial.get('ethnicity'):
        traits.append(facial['ethnicity'])
    if facial.get('hair_color') and facial.get('hair_type'):
        traits.append(f"{facial['hair_color']} {facial['hair_type']} hair")
    if body.get('body_type'):
        traits.append(f"{body['body_type']} build")
    if body.get('height_cm'):
        traits.append(f"{body['height_cm']}cm")
    
    return ", ".join(traits) if traits else "person"


def build_garment_instruction(product, image_index, has_top, has_dress):
    """Construir instrucción específica para cada prenda"""
    name = product.get('name', 'item')
    ref = f"@image {image_index}"
    category = product.get('category', 'other')
    
    instructions = {
        'top': f"Replace white t-shirt with {name} from {ref} (base layer)",
        'bottom': f"Replace blue jeans with {name} from {ref}",
        'outerwear': f"Add {name} from {ref} as outer layer" + (" (over shirt)" if has_top else ""),
        'dress': f"Replace ALL clothing with {name} from {ref} (complete outfit)",
        'set': f"Replace ALL clothing with {name} from {ref} (complete outfit)",
        'shoes': f"Replace footwear with {name} from {ref}",
        'footwear': f"Replace footwear with {name} from {ref}",
        'accessories': f"Add accessory {name} from {ref}",
        'head': f"Add {name} from {ref} on head/face",
        'bag': f"Add {name} from {ref} as handbag",
        'jewelry': f"Add {name} from {ref} as jewelry",
    }
    
    return instructions.get(category, f"Add {name} from {ref}")


def determine_auto_background(products):
    """Determinar background automático basado en outfit"""
    categories = [p.get('category') for p in products]
    
    if 'outerwear' in categories:
        return "urban city street, modern architecture, natural daylight, depth of field"
    if 'dress' in categories or 'set' in categories:
        return "elegant interior, soft ambient light, luxury fashion setting"
    if 'shoes' in categories or 'footwear' in categories:
        return "minimalist floor, soft gradient, focus on full body"
    return "clean studio, neutral tones, professional lighting"


def build_concat_tryon_prompt(products_metadata, settings, avatar_info, num_garments):
    """
    Construir prompt para imagen concatenada (Avatar + Prendas lado a lado)
    FLUX.2 verá la imagen completa y seguirá las instrucciones
    """
    
    # Descripción del avatar
    model_desc = "person"
    if avatar_info:
        facial = avatar_info.get('grok_facial_features', {})
        body = avatar_info.get('grok_body_analysis', {})
        gender = facial.get('gender_presentation', 'person')
        model_desc = f"{gender}"
        if body.get('body_type'):
            model_desc += f" with {body.get('body_type')} build"
    
    # Describir prendas por posición
    garment_descriptions = []
    for idx, product in enumerate(products_metadata):
        name = product.get('name', 'clothing item')
        category = product.get('category', 'item')
        position = idx + 2  # 2, 3, 4... (1 es avatar)
        
        if num_garments == 1:
            pos_desc = "on the right side"
        else:
            if idx == 0:
                pos_desc = "second from left"
            elif idx == num_garments - 1:
                pos_desc = "on the far right"
            else:
                pos_desc = f"in position {position}"
        
        garment_descriptions.append(f"- {name} ({category}) shown {pos_desc}")
    
    # Background y settings
    bg_desc = "pure white seamless studio background"
    pose_desc = "standing straight, facing camera, arms relaxed"
    lighting_desc = "soft professional studio lighting"
    color_accent = ""
    
    if settings:
        bg = settings.get('background', {})
        if bg.get('prompt_addon'):
            bg_desc = bg.get('prompt_addon')
        if settings.get('backgroundColor') and settings.get('backgroundColor') != '#FFFFFF':
            color_accent = f"\nAmbient color accent: {settings.get('backgroundColor')}"
        if settings.get('pose', {}).get('prompt_addon'):
            pose_desc = settings['pose']['prompt_addon']
        if settings.get('lighting', {}).get('prompt_addon'):
            lighting_desc = settings['lighting']['prompt_addon']
    
    prompt = f"""This is a composite reference image showing multiple elements side by side.

LEFT SIDE: A {model_desc} (the avatar/model) wearing basic neutral clothing.
RIGHT SIDE(S): Product images of clothing items to apply:
{chr(10).join(garment_descriptions)}

TASK: Generate a NEW single image of ONLY the person from the LEFT, now wearing ALL the clothing items shown on the RIGHT.

CRITICAL REQUIREMENTS:
1. OUTPUT must show ONLY the person, NOT the original composite layout
2. PRESERVE the exact face, skin tone, body shape, and proportions from the left avatar
3. APPLY each garment exactly as shown in its reference (same colors, patterns, design, fit)
4. Garments must fit naturally on the body with realistic folds and draping
5. Full body visible from head to feet in portrait orientation (9:16 ratio)

SCENE:
- Background: {bg_desc}{color_accent}
- Pose: {pose_desc}
- Lighting: {lighting_desc}

STYLE: Fashion photography, commercial quality, 4K sharp focus, photorealistic."""
    
    return prompt


def render_tryon_prompt(prompt_data):
    """Renderizar promptData a texto para FLUX.2"""
    subject = prompt_data["subject"]
    garments = prompt_data["garments"]
    scene = prompt_data["scene"]
    technical = prompt_data["technical"]
    
    # Image references
    image_refs = [
        f"{subject['base_image']} is the base avatar ({subject['model']} wearing {subject['base_clothing']})."
    ]
    for g in garments:
        image_refs.append(f"{g['image_ref']} is {g['name']} ({g['category']}).")
    
    # Instructions
    instructions = [g['instruction'] for g in garments]
    
    # Color accent line
    color_line = ""
    if scene.get('color_accent') and scene['color_accent'] != '#FFFFFF':
        color_line = f"\n- Ambient color tone: {scene['color_accent']} as predominant accent"
    
    prompt = f"""IMAGE REFERENCES:
{chr(10).join(image_refs)}

EDIT INSTRUCTIONS:
{chr(10).join(instructions)}

SCENE CONFIGURATION:
- Background: {scene['background']}{color_line}
- Model pose: {scene['pose']}
- Lighting: {scene['lighting']}

PRESERVATION REQUIREMENTS:
- Face: Preserve EXACT facial features, expression, skin tone from {subject['base_image']}
- Body: Maintain identical proportions and physique
- Identity: Person must be recognizable as same individual

TECHNICAL SPECIFICATIONS:
- Quality: {technical['quality']}
- Style: {technical['style']}
- Composition: Full body visible, head to feet
- Garment fit: Natural draping, realistic folds and shadows"""
    
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
                "unet_name": UNET_CONFIG["name"] if UNET_CONFIG else "flux2_dev_fp8mixed.safetensors",
                "weight_dtype": UNET_CONFIG["dtype"] if UNET_CONFIG else "default"
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
        
        # === GUIDER (directo sin ReferenceLatent) ===
        "22": {
            "inputs": {
                "model": ["12", 0],
                "conditioning": ["26", 0]
            },
            "class_type": "BasicGuider"
        },
        
        # === SAMPLER ===
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
                "steps": 15,  # Reducido para velocidad
                "denoise": 0.55,
                "width": 1024,
                "height": 1024
            },
            "class_type": "Flux2Scheduler"
        },
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
    update_job_progress(job_id, 15, "Enviando a GPU...")
    
    payload = {"prompt": workflow}
    resp = requests.post(f"{COMFY_URL}/prompt", json=payload, timeout=60)
    resp.raise_for_status()
    
    prompt_id = resp.json()['prompt_id']
    print(f"📤 [Job {job_id}] ComfyUI prompt_id: {prompt_id}")
    
    update_job_progress(job_id, 20, "Procesando en GPU...")
    
    # Esperar resultado con actualizaciones de progreso
    result_path = wait_for_comfy_result(job_id, prompt_id, '9', max_wait=120, total_steps=20)
    
    print(f"✅ [Job {job_id}] Face enhancement completado: {result_path}")
    return result_path


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
                "unet_name": UNET_CONFIG["name"] if UNET_CONFIG else "flux2_dev_fp8mixed.safetensors",
                "weight_dtype": UNET_CONFIG["dtype"] if UNET_CONFIG else "default"
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
        
        # === VAE ENCODE (referencia facial) ===
        "40": {
            "inputs": {
                "pixels": ["41", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        },
        
        # === GUIDER (directo sin ReferenceLatent) ===
        "22": {
            "inputs": {
                "model": ["12", 0],
                "conditioning": ["26", 0]
            },
            "class_type": "BasicGuider"
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
        
        # === SAMPLER ===
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
                "steps": 20,  # Reducido de 25 para velocidad
                "denoise": 1.0,  # Full generation
                "width": 1024,
                "height": 1536
            },
            "class_type": "Flux2Scheduler"
        },
        "13": {
            "inputs": {
                "noise": ["25", 0],
                "guider": ["22", 0],
                "sampler": ["16", 0],
                "sigmas": ["48", 0],
                "latent_image": ["47", 0]
            },
            "class_type": "SamplerCustomAdvanced"
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
    update_job_progress(job_id, 15, "Enviando a GPU...")
    
    payload = {"prompt": workflow}
    resp = requests.post(f"{COMFY_URL}/prompt", json=payload, timeout=60)
    resp.raise_for_status()
    
    prompt_id = resp.json()['prompt_id']
    print(f"📤 [Job {job_id}] ComfyUI prompt_id: {prompt_id}")
    
    update_job_progress(job_id, 20, "Generando avatar...")
    
    # Esperar resultado con actualizaciones de progreso (25 steps, ~70s)
    result_path = wait_for_comfy_result(job_id, prompt_id, '9', max_wait=180, total_steps=25)
    
    print(f"✅ [Job {job_id}] Avatar base generado: {result_path}")
    return result_path


def execute_flux_direct(job):
    """
    Ejecutar workflow de ComfyUI para try-on con FLUX Kontext
    Usa los nodos correctos en inglés:
    - FluxKontextImageScale (escalar imágenes)
    - ReferenceLatent (referencia de latentes)
    - EditModelReferenceMethod (método multi-referencia)
    """
    
    job_id = job['id']
    
    print(f"🎬 [Job {job_id}] Ejecutando try-on FLUX Kontext...")
    
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    Path(COMFY_INPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    # 1. Descargar avatar
    avatar_url = job['input_data']['avatar_url']
    avatar_filename = f"avatar_{job_id}.jpg"
    avatar_path = f"{COMFY_INPUT_DIR}/{avatar_filename}"
    print(f"📥 [Job {job_id}] Descargando avatar...")
    download_image(avatar_url, avatar_path)
    
    # 2. Descargar prendas (cada una por separado)
    MAX_PRODUCTS = 5
    garments = job['input_data'].get('garment_images', [])
    garment_filenames = []
    
    products_to_process = min(len(garments), MAX_PRODUCTS)
    print(f"👗 [Job {job_id}] Descargando {products_to_process} prendas...")
    
    for idx, garment in enumerate(garments[:MAX_PRODUCTS]):
        filename = f"garment_{job_id}_{idx}.jpg"
        path = f"{COMFY_INPUT_DIR}/{filename}"
        download_image(garment['url'], path)
        garment_filenames.append(filename)
        print(f"   → image {idx + 2}: {filename}")
    
    # 3. Obtener settings y avatar info
    settings = job['input_data'].get('settings', None)
    avatar_info = None
    try:
        user_id = job['user_id']
        resp = supabase.table('virtual_avatars').select(
            'grok_facial_features, grok_body_analysis'
        ).eq('user_id', user_id).maybe_single().execute()
        if resp.data:
            avatar_info = resp.data
    except Exception as e:
        print(f"⚠️ [Job {job_id}] No avatar info: {e}")
    
    # 4. Construir prompt
    products_metadata = job['input_data'].get('products_metadata', [])[:MAX_PRODUCTS]
    prompt = build_tryon_prompt_comfyui(products_metadata, settings, avatar_info)
    
    print(f"\n📝 [Job {job_id}] PROMPT:\n{prompt[:500]}...")
    
    seed = int(time.time()) % 999999999
    
    # =====================================================
    # WORKFLOW FLUX KONTEXT - Nodos en inglés
    # Avatar + cada prenda → FluxKontextImageScale → VAE → ReferenceLatent
    # Encadenados → EditModelReferenceMethod → Guider → Sampler
    # =====================================================
    
    unet_name = UNET_CONFIG["name"] if UNET_CONFIG else "flux2_dev_fp8mixed.safetensors"
    unet_dtype = UNET_CONFIG["dtype"] if UNET_CONFIG else "default"
    print(f"   🔧 [Job {job_id}] Usando modelo: {unet_name} (dtype: {unet_dtype})")
    
    workflow = {
        # === MODELOS ===
        "12": {
            "inputs": {
                "unet_name": unet_name,
                "weight_dtype": unet_dtype
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
                "guidance": 3.5,
                "conditioning": ["6", 0]
            },
            "class_type": "FluxGuidance"
        },
        
        # === IMAGE 1: AVATAR ===
        "42": {
            "inputs": {
                "image": avatar_filename,
                "upload": "image"
            },
            "class_type": "LoadImage"
        },
        "60": {
            "inputs": {
                "image": ["42", 0]
            },
            "class_type": "FluxKontextImageScale"
        },
        "40": {
            "inputs": {
                "pixels": ["60", 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        },
        # ReferenceLatent 1: Avatar
        "39": {
            "inputs": {
                "conditioning": ["26", 0],
                "latent": ["40", 0]
            },
            "class_type": "ReferenceLatent"
        },
        
        # === SAMPLING ===
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
                "steps": 25,  # 25 steps = alta calidad
                "denoise": 0.50,  # Bajo para preservar referencias
                "width": 1440,
                "height": 2560  # 2K HD 9:16
            },
            "class_type": "Flux2Scheduler"
        },
    }
    
    # === AÑADIR CADA PRENDA COMO REFERENCIA ===
    last_ref_node = "39"
    
    for idx, garment_filename in enumerate(garment_filenames):
        load_id = f"g{idx}_load"
        scale_id = f"g{idx}_scale"
        encode_id = f"g{idx}_encode"
        ref_id = f"g{idx}_ref"
        
        # LoadImage
        workflow[load_id] = {
            "inputs": {
                "image": garment_filename,
                "upload": "image"
            },
            "class_type": "LoadImage"
        }
        
        # FluxKontextImageScale
        workflow[scale_id] = {
            "inputs": {
                "image": [load_id, 0]
            },
            "class_type": "FluxKontextImageScale"
        }
        
        # VAEEncode
        workflow[encode_id] = {
            "inputs": {
                "pixels": [scale_id, 0],
                "vae": ["10", 0]
            },
            "class_type": "VAEEncode"
        }
        
        # ReferenceLatent encadenado
        workflow[ref_id] = {
            "inputs": {
                "conditioning": [last_ref_node, 0],
                "latent": [encode_id, 0]
            },
            "class_type": "ReferenceLatent"
        }
        
        last_ref_node = ref_id
        print(f"   📎 Prenda {idx + 1}: {garment_filename}")
    
    # === GUIDER (conecta directamente al último ReferenceLatent) ===
    workflow["22"] = {
        "inputs": {
            "model": ["12", 0],
            "conditioning": [last_ref_node, 0]
        },
        "class_type": "BasicGuider"
    }
    
    # === SAMPLER (usa latente del avatar) ===
    workflow["13"] = {
        "inputs": {
            "noise": ["25", 0],
            "guider": ["22", 0],
            "sampler": ["16", 0],
            "sigmas": ["48", 0],
            "latent_image": ["40", 0]
        },
        "class_type": "SamplerCustomAdvanced"
    }
    
    # === DECODE ===
    workflow["8"] = {
        "inputs": {
            "samples": ["13", 0],
            "vae": ["10", 0]
        },
        "class_type": "VAEDecode"
    }
    
    # === SAVE ===
    workflow["9"] = {
        "inputs": {
            "filename_prefix": f"tryon_{job_id}",
            "images": ["8", 0]
        },
        "class_type": "SaveImage"
    }
    
    print(f"\n📊 [Job {job_id}] Workflow FLUX Kontext:")
    print(f"   image 1: Avatar ({avatar_filename})")
    for idx, gf in enumerate(garment_filenames):
        print(f"   image {idx+2}: {gf}")
    print(f"   Referencias: {1 + len(garment_filenames)}")
    
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
    
    update_job_progress(job_id, 20, "Procesando en GPU...")
    
    # Esperar resultado con actualizaciones de progreso
    result_path = wait_for_comfy_result(job_id, prompt_id, '9', max_wait=300, total_steps=20)
    
    print(f"✅ [Job {job_id}] Imagen generada: {result_path}")
    return result_path

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
            print(f"✅ [Job {job_id}] Face HD guardada en profiles.face_hd_url")
        elif job_type == 'avatar_generation':
            # Actualizar virtual_avatars con el avatar base
            # Primero verificar si existe el registro
            existing = supabase.table('virtual_avatars').select('id').eq('user_id', user_id).execute()
            
            if existing.data and len(existing.data) > 0:
                # Actualizar registro existente
                supabase.table('virtual_avatars').update({
                    'base_avatar_url': public_url,
                    'base_avatar_generated_at': datetime.utcnow().isoformat(),
                    'base_avatar_status': 'completed',
                    'updated_at': datetime.utcnow().isoformat()
                }).eq('user_id', user_id).execute()
            else:
                # Crear nuevo registro
                supabase.table('virtual_avatars').insert({
                    'user_id': user_id,
                    'base_avatar_url': public_url,
                    'base_avatar_generated_at': datetime.utcnow().isoformat(),
                    'base_avatar_status': 'completed'
                }).execute()
            
            # También guardar en profiles como fallback
            supabase.table('profiles').update({
                'avatar_url': public_url,
                'avatar_generated_at': datetime.utcnow().isoformat()
            }).eq('id', user_id).execute()
            print(f"✅ [Job {job_id}] Avatar guardado en virtual_avatars.base_avatar_url")
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
    
    # Detectar mejor modelo
    global UNET_CONFIG
    UNET_CONFIG = get_optimal_unet_config()
    print(f"   ⚡ Modelo: {UNET_CONFIG['name']} (dtype: {UNET_CONFIG['dtype']})")
    
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

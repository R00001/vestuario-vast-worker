# 🎬 IMPLEMENTACIÓN: Video Try-On con FLUX Klein + LTX-2.3

> Documento de implementación quirúrgica para añadir Video Lookbook al sistema de Try-On de LOOKS.
> Fecha: 2026-03-09

---

## 📊 ESTADO ACTUAL DEL SISTEMA

### Worker actual (`vestuario-vast-worker/worker_vast.py`)
- **Modelo base**: FLUX.2 dev NVFP4 (~6GB VRAM)
- **CLIP**: mistral_3_small_flux2_bf16.safetensors
- **VAE**: flux2-vae.safetensors
- **Técnica**: FLUX Kontext multi-referencia (avatar + N prendas → ReferenceLatent encadenados)
- **Output**: Imagen estática del try-on
- **Custom nodes**: ComfyUI-FLUX, ComfyUI-KJNodes, ComfyUI-Custom-Scripts

### Flujo actual App → Worker
1. App crea job en `ai_generation_jobs` (`job_type: 'tryon'`, `preferred_backend: 'vast'`)
2. Worker pollea Supabase cada 5s, toma jobs pendientes
3. Worker ejecuta `execute_flux_direct()` → ComfyUI genera imagen
4. Resultado → Supabase Storage → `tryon_results`
5. App recibe updates vía **Supabase Realtime** (`subscribeToUserJobs` + `subscribeToTryonResults`)

### Provisioning actual (`provision-looks.sh`)
- Template Vast: `vastai/comfy` con `flux.2-dev.sh`
- Descarga FLUX.2 dev NVFP4 (~12GB)
- Instala custom nodes
- Clona worker desde GitHub repo
- Configura supervisor para auto-start

---

## 🎯 NUEVOS MODELOS

### 1. Try-On: `fal/flux-klein-9b-virtual-tryon-lora`
- **HuggingFace**: https://huggingface.co/fal/flux-klein-9b-virtual-tryon-lora
- **Base model**: FLUX.2 Klein 9B Edit (`black-forest-labs/FLUX.2-klein-base-9B`)
- **Tipo**: LoRA (ficheros pequeños ~hundreds MB)
- **Input**: 3 imágenes (persona + top + bottom) + prompt con trigger `TRYON`
- **Output**: Persona vistiendo las prendas
- **ComfyUI weights**: `flux-klein-tryon-comfy.safetensors`
- **Settings recomendados**: steps=28, guidance=2.5, lora_scale=1.0

### 2. Try-Off: `fal/virtual-tryoff-lora`
- **HuggingFace**: https://huggingface.co/fal/virtual-tryoff-lora
- **Base model**: FLUX.2 Klein 9B (`black-forest-labs/FLUX.2-klein-9B`)
- **Tipo**: LoRA
- **Input**: 1 imagen de persona vistiendo ropa + prompt con trigger `TRYOFF`
- **Output**: Prenda aislada sobre fondo blanco (invisible mannequin)
- **ComfyUI weights**: `virtual-tryoff-lora_comfy.safetensors`
- **Uso**: Feature de Wardrobe (extraer productos de fotos subidas)

### 3. Video: LTX-2.3 (`Lightricks/LTX-2.3`)
- **HuggingFace**: https://huggingface.co/Lightricks/LTX-2.3
- **Tipo**: Modelo image-to-video completo
- **Input**: Imagen de referencia + prompt de video
- **Output**: Video 5-20s, hasta 1080p, 24-50 FPS
- **Tamaño modelo**: ~15-22GB (distilled)
- **VRAM necesaria**: ~15-22GB
- **Portrait nativo**: Soporta 9:16

---

## ✅ ANÁLISIS DE VRAM — RTX 6000 96GB

### GPU disponible: RTX 6000 Ada — 96GB VRAM

Con 96GB, **TODOS los modelos caben cargados SIMULTÁNEAMENTE** en VRAM.
No hay swap, no hay descarga/recarga, CERO latencia extra.

### Uso de VRAM (todo cargado a la vez):

| Modelo | VRAM (bf16) | Notas |
|--------|-------------|-------|
| FLUX Klein 9B (base) | ~18GB | bf16 nativo, máxima calidad |
| Try-On LoRA | ~0.5GB | Se aplica sobre Klein |
| Try-Off LoRA | ~0.5GB | Se aplica sobre Klein |
| CLIP (Mistral/T5) | ~3GB | Shared entre Klein y LTX |
| VAE (FLUX + LTX) | ~1GB | |
| LTX-2.3 **22B** distilled | **~44GB** | 22B params, 8 steps, cargado permanente |
| **TOTAL en VRAM** | **~67GB** | |
| **LIBRE** | **~29GB** | Suficiente para intermedios/batches |

### Conclusión: SIN SWAP, MÁXIMA VELOCIDAD

**53-58GB libres** después de cargar todo. Los modelos quedan residentes en VRAM.
ComfyUI NO necesita descargar nada entre workflows. Velocidad máxima de inferencia.

No se usa fp8 ni cuantización — bf16 nativo para máxima calidad de imagen y video.

---

## 🏗️ ARQUITECTURA: TODO LOCAL EN GPU 96GB — ZERO SWAP

```
App → Worker → ComfyUI Workflow 1 (Klein Try-On) → Imagen → App (inmediato)
                     ↓ (sin swap — ambos modelos ya cargados)
               ComfyUI Workflow 2 (LTX-2.3 Video) → Video → App (~30s después)
```

- ✅ 100% local — sin dependencia de APIs externas
- ✅ AMBOS modelos cargados SIMULTÁNEAMENTE en 96GB VRAM
- ✅ ZERO latencia de swap — inferencia directa
- ✅ bf16 nativo — máxima calidad (no necesitamos cuantizar)
- ✅ Costo mínimo por uso (~$0.01 GPU time total)
- ✅ Worker envía imagen ANTES de generar video (UX inmediata)
- ✅ Video de 4-6s genera en ~20-40s (sin swap overhead)

### Custom nodes necesarios
- **ComfyUI-LTXVideo** (`Lightricks/ComfyUI-LTXVideo`) — nodos para LTX-2.3 i2v
- Los nodos FLUX existentes ya soportan Klein + LoRA

### Configuración ComfyUI para mantener modelos en VRAM
```bash
# Arrancar ComfyUI con --highvram para que NO descargue modelos
python main.py --listen 0.0.0.0 --port 18188 --highvram
```
El flag `--highvram` le dice a ComfyUI que mantenga TODOS los modelos en VRAM
sin descargar. Con 96GB sobra espacio para Klein + LTX + intermedios.

---

## 📋 PLAN DE IMPLEMENTACIÓN (Opción B)

### FASE 1: Migración de Base de Datos

#### 1.1 Añadir `video_url` a `tryon_results`

```sql
ALTER TABLE tryon_results
ADD COLUMN video_url TEXT DEFAULT NULL;

ALTER TABLE tryon_results
ADD COLUMN video_status TEXT DEFAULT NULL
CHECK (video_status IN ('generating', 'completed', 'failed', NULL));
```

#### 1.2 No se necesitan más cambios de esquema
- `ai_generation_jobs.result_metadata` (JSONB) ya soporta campos extra
- Usaremos `result_metadata.video_status` y `result_metadata.video_url` para updates en tiempo real

---

### FASE 2: Provisioning — Swap FLUX.2 dev → Klein 9B + LoRAs

#### 2.1 Cambios en `provision-looks.sh`

Reemplazar PASO 1.4 (descarga modelo NVFP4) con descarga de Klein 9B + LoRAs:

```bash
# ============================================================
# PASO 1.4: Descargar FLUX Klein 9B + LoRAs para Try-On/Try-Off
# ============================================================
echo ""
echo "⚡ PASO 1.4: Descargando FLUX Klein 9B + LoRAs..."
echo ""

MODELS_DIR="/workspace/ComfyUI/models/diffusion_models"
LORAS_DIR="/workspace/ComfyUI/models/loras"

# --- FLUX Klein 9B base model ---
KLEIN_MODEL="flux2-klein-9b.safetensors"
if [ ! -f "$MODELS_DIR/$KLEIN_MODEL" ]; then
  echo "   Descargando FLUX Klein 9B base (~18GB bf16)..."
  cd "$MODELS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/black-forest-labs/FLUX.2-klein-9B/resolve/main/flux2-klein-9b.safetensors" \
    -O "$KLEIN_MODEL" 2>&1 | tail -n 1 || {
    echo "⚠️ Error descargando Klein 9B"
  }
  cd /workspace
else
  echo "   ✓ Klein 9B ya existe"
fi

# --- Try-On LoRA (ComfyUI version) ---
TRYON_LORA="flux-klein-tryon-comfy.safetensors"
if [ ! -f "$LORAS_DIR/$TRYON_LORA" ]; then
  echo "   Descargando Try-On LoRA..."
  cd "$LORAS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/fal/flux-klein-9b-virtual-tryon-lora/resolve/main/flux-klein-tryon-comfy.safetensors" \
    -O "$TRYON_LORA" 2>&1 | tail -n 1
  cd /workspace
else
  echo "   ✓ Try-On LoRA ya existe"
fi

# --- Try-Off LoRA (ComfyUI version) ---
TRYOFF_LORA="virtual-tryoff-lora_comfy.safetensors"
if [ ! -f "$LORAS_DIR/$TRYOFF_LORA" ]; then
  echo "   Descargando Try-Off LoRA..."
  cd "$LORAS_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/fal/virtual-tryoff-lora/resolve/main/virtual-tryoff-lora_comfy.safetensors" \
    -O "$TRYOFF_LORA" 2>&1 | tail -n 1
  cd /workspace
else
  echo "   ✓ Try-Off LoRA ya existe"
fi

echo "✅ [$(date)] Modelos Klein + LoRAs listos"
echo ""
echo "📋 Modelos disponibles:"
ls -lh "$MODELS_DIR"/*.safetensors 2>/dev/null
echo "📋 LoRAs disponibles:"
ls -lh "$LORAS_DIR"/*.safetensors 2>/dev/null
```

#### 2.2 Configurar ComfyUI con `--highvram`

En el provisioning (o config de supervisor de ComfyUI), asegurar que ComfyUI
arranca con `--highvram` para que mantenga TODOS los modelos cargados en los 96GB:

```bash
# En el script de arranque de ComfyUI del template, añadir --highvram:
python main.py --listen 0.0.0.0 --port 18188 --highvram
```

No se necesitan nuevas variables de entorno. Las mismas `WORKER_ID`, `SUPABASE_URL`,
`SUPABASE_KEY` existentes son suficientes. Todo corre local en ComfyUI.

---

### FASE 3: Worker — Nuevo modelo de detección y Try-On con Klein LoRA

#### 3.1 Actualizar `get_optimal_unet_config()` para Klein

```python
def get_optimal_unet_config():
    """Detecta el mejor modelo disponible (prioriza Klein 9B para try-on)"""
    models_dir = "/workspace/ComfyUI/models/diffusion_models"
    loras_dir = "/workspace/ComfyUI/models/loras"
    
    print(f"\n🔍 Buscando modelos en: {models_dir}")
    if os.path.exists(models_dir):
        models = [f for f in os.listdir(models_dir) if f.endswith('.safetensors')]
        print(f"   Modelos encontrados: {models}")
    
    if os.path.exists(loras_dir):
        loras = [f for f in os.listdir(loras_dir) if f.endswith('.safetensors')]
        print(f"   LoRAs encontrados: {loras}")
    
    # Prioridad: Klein 9B (para LoRAs try-on/try-off) > NVFP4 > fp8
    if os.path.exists(f"{models_dir}/flux2-klein-9b.safetensors"):
        has_tryon_lora = os.path.exists(f"{loras_dir}/flux-klein-tryon-comfy.safetensors")
        has_tryoff_lora = os.path.exists(f"{loras_dir}/virtual-tryoff-lora_comfy.safetensors")
        print(f"   ⚡ Seleccionado: Klein 9B (try-on LoRA: {has_tryon_lora}, try-off LoRA: {has_tryoff_lora})")
        return {
            "name": "flux2-klein-9b.safetensors",
            "dtype": "default",  # bf16 nativo
            "model_type": "klein",
            "has_tryon_lora": has_tryon_lora,
            "has_tryoff_lora": has_tryoff_lora,
        }
    elif os.path.exists(f"{models_dir}/flux2-dev-nvfp4.safetensors"):
        print("   ⚡ Seleccionado: NVFP4 puro (fallback, sin LoRA try-on)")
        return {"name": "flux2-dev-nvfp4.safetensors", "dtype": "default", "model_type": "dev"}
    elif os.path.exists(f"{models_dir}/flux2_dev_fp8mixed.safetensors"):
        print("   📦 Seleccionado: fp8 (del template)")
        return {"name": "flux2_dev_fp8mixed.safetensors", "dtype": "fp8_e4m3fn_fast", "model_type": "dev"}
    else:
        print("   ⚠️ Ningún modelo encontrado, usando default")
        return {"name": "flux2_dev_fp8mixed.safetensors", "dtype": "default", "model_type": "dev"}
```

#### 3.2 Nueva función: `execute_klein_tryon(job)`

```python
def execute_klein_tryon(job):
    """
    Try-On con FLUX Klein 9B + LoRA fal/flux-klein-9b-virtual-tryon-lora
    
    Input: avatar (person) + hasta 2 prendas (top + bottom)
    Prompt: TRYON [description]. Replace outfit with [top] and [bottom]...
    """
    
    job_id = job['id']
    print(f"👗 [Job {job_id}] Ejecutando Try-On con Klein LoRA...")
    
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    Path(COMFY_INPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    # 1. Descargar avatar (persona)
    avatar_url = job['input_data']['avatar_url']
    avatar_filename = f"person_{job_id}.jpg"
    avatar_path = f"{COMFY_INPUT_DIR}/{avatar_filename}"
    download_image(avatar_url, avatar_path)
    
    # 2. Descargar prendas (máx 2: top + bottom)
    garments = job['input_data'].get('garment_images', [])
    products_metadata = job['input_data'].get('products_metadata', [])
    
    # Separar top y bottom
    top_filename = None
    bottom_filename = None
    top_desc = "current top"
    bottom_desc = "current bottom"
    
    for idx, garment in enumerate(garments[:2]):
        filename = f"garment_{job_id}_{idx}.jpg"
        path = f"{COMFY_INPUT_DIR}/{filename}"
        download_image(garment['url'], path)
        
        category = garment.get('category', products_metadata[idx].get('category', 'top') if idx < len(products_metadata) else 'top')
        name = products_metadata[idx].get('name', 'clothing') if idx < len(products_metadata) else 'clothing'
        
        if category in ['top', 'outerwear', 'dress', 'set']:
            top_filename = filename
            top_desc = name
        elif category in ['bottom', 'shoes', 'footwear']:
            bottom_filename = filename
            bottom_desc = name
        else:
            # Default: primero es top, segundo es bottom
            if top_filename is None:
                top_filename = filename
                top_desc = name
            else:
                bottom_filename = filename
                bottom_desc = name
    
    # Si solo hay una prenda, la otra queda como la actual
    if top_filename is None:
        top_filename = avatar_filename  # Usar avatar como referencia de top
        top_desc = "keep current top unchanged"
    if bottom_filename is None:
        bottom_filename = avatar_filename
        bottom_desc = "keep current bottom unchanged"
    
    # 3. Construir prompt
    avatar_info = None
    try:
        user_id = job['user_id']
        resp = supabase.table('virtual_avatars').select(
            'grok_facial_features, grok_body_analysis'
        ).eq('user_id', user_id).maybe_single().execute()
        if resp.data:
            avatar_info = resp.data
    except:
        pass
    
    person_desc = build_model_description(avatar_info) if avatar_info else "person"
    
    prompt = f"TRYON {person_desc}, standing casually. Replace the outfit with {top_desc} and {bottom_desc} as shown in the reference images. The final image is a full body shot."
    
    print(f"📝 [Job {job_id}] Prompt: {prompt}")
    
    seed = int(time.time()) % 999999999
    
    # 4. Workflow ComfyUI con Klein + LoRA
    workflow = {
        # === MODELOS ===
        "12": {
            "inputs": {
                "unet_name": UNET_CONFIG["name"],
                "weight_dtype": UNET_CONFIG["dtype"]
            },
            "class_type": "UNETLoader"
        },
        # === LoRA Try-On ===
        "70": {
            "inputs": {
                "lora_name": "flux-klein-tryon-comfy.safetensors",
                "strength_model": 1.0,
                "model": ["12", 0]
            },
            "class_type": "LoraLoaderModelOnly"
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
        
        # === PROMPT con trigger TRYON ===
        "6": {
            "inputs": {
                "text": prompt,
                "clip": ["38", 0]
            },
            "class_type": "CLIPTextEncode"
        },
        "26": {
            "inputs": {
                "guidance": 2.5,  # Recomendado para Klein LoRA
                "conditioning": ["6", 0]
            },
            "class_type": "FluxGuidance"
        },
        
        # === IMÁGENES: Persona, Top, Bottom ===
        # Imagen 1: Persona
        "42": {
            "inputs": {"image": avatar_filename},
            "class_type": "LoadImage"
        },
        "60": {
            "inputs": {"megapixels": 1.0, "image": ["42", 0]},
            "class_type": "FluxKontextImageScale"
        },
        "40": {
            "inputs": {"pixels": ["60", 0], "vae": ["10", 0]},
            "class_type": "VAEEncode"
        },
        "39": {
            "inputs": {"conditioning": ["26", 0], "latent": ["40", 0]},
            "class_type": "ReferenceLatent"
        },
        
        # Imagen 2: Top garment
        "50": {
            "inputs": {"image": top_filename},
            "class_type": "LoadImage"
        },
        "51": {
            "inputs": {"megapixels": 1.0, "image": ["50", 0]},
            "class_type": "FluxKontextImageScale"
        },
        "52": {
            "inputs": {"pixels": ["51", 0], "vae": ["10", 0]},
            "class_type": "VAEEncode"
        },
        "53": {
            "inputs": {"conditioning": ["39", 0], "latent": ["52", 0]},
            "class_type": "ReferenceLatent"
        },
        
        # Imagen 3: Bottom garment
        "54": {
            "inputs": {"image": bottom_filename},
            "class_type": "LoadImage"
        },
        "55": {
            "inputs": {"megapixels": 1.0, "image": ["54", 0]},
            "class_type": "FluxKontextImageScale"
        },
        "56": {
            "inputs": {"pixels": ["55", 0], "vae": ["10", 0]},
            "class_type": "VAEEncode"
        },
        "57": {
            "inputs": {"conditioning": ["53", 0], "latent": ["56", 0]},
            "class_type": "ReferenceLatent"
        },
        
        # === SAMPLING ===
        "25": {
            "inputs": {"noise_seed": seed},
            "class_type": "RandomNoise"
        },
        "16": {
            "inputs": {"sampler_name": "euler"},
            "class_type": "KSamplerSelect"
        },
        "48": {
            "inputs": {
                "steps": 28,  # Recomendado para Klein LoRA
                "denoise": 0.85,
                "width": 768,
                "height": 1024  # Portrait
            },
            "class_type": "Flux2Scheduler"
        },
        
        # === GUIDER con modelo + LoRA ===
        "22": {
            "inputs": {
                "model": ["70", 0],  # Modelo con LoRA aplicado
                "conditioning": ["57", 0]  # Último ReferenceLatent
            },
            "class_type": "BasicGuider"
        },
        
        # === SAMPLER ===
        "13": {
            "inputs": {
                "noise": ["25", 0],
                "guider": ["22", 0],
                "sampler": ["16", 0],
                "sigmas": ["48", 0],
                "latent_image": ["40", 0]  # Base: avatar
            },
            "class_type": "SamplerCustomAdvanced"
        },
        
        # === DECODE Y GUARDAR ===
        "8": {
            "inputs": {"samples": ["13", 0], "vae": ["10", 0]},
            "class_type": "VAEDecode"
        },
        "9": {
            "inputs": {
                "filename_prefix": f"tryon_{job_id}",
                "images": ["8", 0]
            },
            "class_type": "SaveImage"
        }
    }
    
    # Enviar a ComfyUI
    update_job_progress(job_id, 15, "Enviando a GPU (Klein LoRA)...")
    
    payload = {"prompt": workflow, "client_id": WORKER_ID}
    resp = requests.post(f"{COMFY_URL}/prompt", json=payload, timeout=30)
    resp.raise_for_status()
    
    prompt_id = resp.json()['prompt_id']
    print(f"📤 [Job {job_id}] ComfyUI prompt_id: {prompt_id}")
    
    update_job_progress(job_id, 20, "Generando look con Klein LoRA...")
    
    result_path = wait_for_comfy_result(job_id, prompt_id, '9', max_wait=120, total_steps=28)
    
    print(f"✅ [Job {job_id}] Try-on Klein completado: {result_path}")
    return result_path
```

---

### FASE 4: Worker — Generación de Video con LTX-2.3 LOCAL (ComfyUI)

#### 4.1 Provisioning: Descargar LTX-2.3 + Custom Nodes

Añadir al `provision-looks.sh`:

```bash
# ============================================================
# PASO 1.6: Descargar LTX-2.3 para video lookbook
# ============================================================
echo ""
echo "🎬 PASO 1.6: Descargando LTX-2.3 para video..."
echo ""

LTX_DIR="/workspace/ComfyUI/models/checkpoints"
LTX_MODEL="ltx-2.3-distilled.safetensors"

if [ ! -f "$LTX_DIR/$LTX_MODEL" ]; then
  echo "   Descargando LTX-2.3 distilled..."
  cd "$LTX_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-distilled.safetensors" \
    -O "$LTX_MODEL" 2>&1 | tail -n 1 || {
    echo "⚠️ Error descargando LTX-2.3, probando dev..."
    wget --progress=bar:force:noscroll \
      "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-dev.safetensors" \
      -O "ltx-2.3-dev.safetensors" 2>&1 | tail -n 1 || {
      echo "⚠️ LTX-2.3 no disponible"
    }
  }
  cd /workspace
else
  echo "   ✓ LTX-2.3 ya existe"
fi

# T5 text encoder para LTX (si no existe ya)
T5_DIR="/workspace/ComfyUI/models/text_encoders"
if [ ! -f "$T5_DIR/google_t5-v1_1-xxl_encoderonly-fp8_e4m3fn.safetensors" ]; then
  echo "   Descargando T5-XXL fp8 para LTX..."
  mkdir -p "$T5_DIR"
  cd "$T5_DIR"
  wget --progress=bar:force:noscroll \
    "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors" \
    -O "t5xxl_fp8_e4m3fn.safetensors" 2>&1 | tail -n 1
  cd /workspace
fi

# Custom nodes para LTX-Video
cd /workspace/ComfyUI/custom_nodes

if [ ! -d "ComfyUI-LTXVideo" ]; then
  echo "   Instalando ComfyUI-LTXVideo..."
  git clone https://github.com/Lightricks/ComfyUI-LTXVideo.git 2>/dev/null || \
    echo "   Repo no disponible, probando alternativa..."
fi

# Instalar dependencias
for dir in */; do
  if [ -f "${dir}requirements.txt" ]; then
    pip install -r "${dir}requirements.txt" 2>/dev/null || true
  fi
done

cd /workspace
echo "✅ [$(date)] LTX-2.3 + custom nodes listos"
```

#### 4.2 Función de video lookbook

```python
import os

def build_lookbook_video_prompt(products_metadata):
    """
    Genera prompt dinámico para LTX-2.3 video lookbook.
    Shot 1 = frontal con cara visible (identidad)
    Shots 2-5 = zooms en items SIN cara (evita pérdida de referencia)
    """
    garments = []
    for p in products_metadata:
        garments.append({
            'name': p.get('name', 'clothing item'),
            'category': p.get('category', 'top'),
        })
    
    # Shot 1 siempre frontal con cara
    shots = [
        "Shot 1 (0-1s): Frontal full-body hero shot matching the reference "
        "pose exactly. Face visible. This shot establishes the identity."
    ]
    
    # Templates por categoría
    shot_templates = {
        'top': "camera at chest height focusing on the top garment. "
               "Frame from shoulders to waist showing fabric detail. Face cropped out.",
        'outerwear': "rear-side camera at roughly 120 degree angle showing "
                     "the back and side silhouette of the jacket. Face not visible.",
        'bottom': "low camera placed near the floor focusing on trousers "
                  "and shoes. Frame from knees to floor. Face not visible.",
        'shoes': "very low camera at floor level, close-up of footwear "
                 "detail. Frame feet and ankles only. Face not visible.",
        'footwear': "very low camera at floor level, close-up of footwear. "
                    "Frame feet and ankles only. Face not visible.",
        'dress': "rear-side camera showing the full dress silhouette from "
                 "behind at 120 degree angle. Face not visible.",
        'set': "camera at hip height showing the full outfit coordination. "
               "Frame from chest to knees. Face cropped out.",
        'accessories': "high fashion camera above shoulder height "
                       "looking down at the accessory detail. Face cropped out.",
        'bag': "camera at hip height focusing on the handbag. "
               "Frame from waist to thighs. Face not visible.",
        'jewelry': "close-up camera focusing on jewelry detail. "
                   "Frame neck/wrist area only. Face not visible.",
    }
    
    fallback_shots = [
        "High fashion camera above shoulder height looking downward "
        "toward the torso. Frame chest to waist. Face cropped out.",
        "Diagonal observer camera from behind the model at third-person "
        "fashion perspective. Frame upper back and shoulders. Face not visible.",
        "Rear-side camera placed behind the model at roughly 120 degree "
        "angle. Shows back silhouette of the outfit. Face not visible.",
    ]
    
    # Generar shots dinámicos basados en prendas
    used_categories = set()
    shot_num = 2
    for g in garments:
        if shot_num > 5:
            break
        cat = g['category']
        if cat in used_categories:
            continue
        template = shot_templates.get(cat, fallback_shots[0])
        time_start = shot_num - 1
        time_end = shot_num
        shots.append(f"Shot {shot_num} ({time_start}-{time_end}s): {template}")
        used_categories.add(cat)
        shot_num += 1
    
    # Rellenar con fallbacks
    fallback_idx = 0
    while shot_num <= 5 and fallback_idx < len(fallback_shots):
        time_start = shot_num - 1
        time_end = shot_num
        shots.append(f"Shot {shot_num} ({time_start}-{time_end}s): {fallback_shots[fallback_idx]}")
        shot_num += 1
        fallback_idx += 1
    
    outfit_desc = ", ".join([g['name'] for g in garments])
    
    prompt = f"""Professional fashion lookbook video generated from the reference image.

The model remains completely still in the same pose as the reference image.
The body posture and gaze direction never change. The model does not react to the cameras.
Only the camera positions change.

The exact same outfit must remain identical in every shot: {outfit_desc}.
IMPORTANT: No garment redesign and no color changes.

The video lasts 5 seconds total with hard cuts between shots.
Each shot uses a different STATIC camera placed around the model.
Cameras do not move. No zoom. No push-in. No orbit.
The model pose and gaze remain frozen across all shots.

SHOT STRUCTURE:
{chr(10).join(shots)}

LIGHTING: Clean editorial studio lighting. Lighting must remain identical across all shots.

CONSTRAINTS:
Same exact outfit instance in every shot. No garment morphing. No color change.
No pose change. No gaze change. No camera movement.
No face visible after the first shot. No artifacts. No flicker."""

    return prompt


def generate_lookbook_video(job_id, tryon_image_path, user_id, products_metadata):
    """
    Generar video lookbook usando LTX-2.3 LOCAL en ComfyUI.
    Con --highvram y 96GB, LTX-2.3 YA está cargado en VRAM.
    Sin swap, inferencia directa.
    
    Input: ruta local de imagen try-on + prompt dinámico
    Output: URL del video subido a Supabase Storage
    """
    
    print(f"🎬 [Job {job_id}] Generando video lookbook con LTX-2.3 LOCAL...")
    
    # La imagen try-on ya está en /workspace/ComfyUI/output/
    # Copiarla a /workspace/ComfyUI/input/ para que LTX la use como referencia
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    video_input_filename = f"tryon_for_video_{job_id}.jpg"
    video_input_path = f"{COMFY_INPUT_DIR}/{video_input_filename}"
    
    import shutil
    shutil.copy2(tryon_image_path, video_input_path)
    
    # Construir prompt dinámico
    prompt = build_lookbook_video_prompt(products_metadata)
    print(f"📝 [Job {job_id}] Video prompt:\n{prompt[:300]}...")
    
    update_job_progress(job_id, 60, "Generando video lookbook (LTX-2.3)...")
    
    seed = int(time.time()) % 999999999
    
    # Detectar qué modelo LTX está disponible
    ltx_model = "ltx-2.3-distilled.safetensors"
    checkpoints_dir = "/workspace/ComfyUI/models/checkpoints"
    if os.path.exists(f"{checkpoints_dir}/ltx-2.3-dev.safetensors"):
        ltx_model = "ltx-2.3-dev.safetensors"
    
    # =====================================================
    # WORKFLOW COMFYUI PARA LTX-2.3 IMAGE-TO-VIDEO
    # ComfyUI descargará Klein automáticamente al cargar LTX
    # =====================================================
    video_workflow = {
        # === CARGAR MODELO LTX-2.3 ===
        "1": {
            "inputs": {
                "ckpt_name": ltx_model
            },
            "class_type": "CheckpointLoaderSimple"
        },
        
        # === CARGAR IMAGEN DE REFERENCIA (try-on result) ===
        "2": {
            "inputs": {
                "image": video_input_filename
            },
            "class_type": "LoadImage"
        },
        
        # === ENCODE PROMPT ===
        "3": {
            "inputs": {
                "text": prompt,
                "clip": ["1", 1]
            },
            "class_type": "CLIPTextEncode"
        },
        
        # === EMPTY LATENT VIDEO (5s @ 25fps = 125 frames) ===
        "4": {
            "inputs": {
                "width": 768,
                "height": 1344,     # 9:16 portrait para fashion
                "length": 121,      # ~5 segundos a 25fps (divisible por 8+1)
                "batch_size": 1
            },
            "class_type": "EmptyLTXVLatentVideo"
        },
        
        # === CONDITIONING CON IMAGEN ===
        "5": {
            "inputs": {
                "positive": ["3", 0],
                "vae": ["1", 2],
                "image": ["2", 0],
                "frame_idx": 0      # Primera frame = imagen try-on
            },
            "class_type": "LTXVConditioning"
        },
        
        # === SAMPLER ===
        "6": {
            "inputs": {
                "seed": seed,
                "steps": 8,         # Distilled = pocos steps
                "cfg": 1.0,         # LTX-2.3 distilled recomienda ~1.0
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["5", 0],
                "negative": ["5", 1],
                "latent_image": ["4", 0]
            },
            "class_type": "KSampler"
        },
        
        # === DECODE VIDEO ===
        "7": {
            "inputs": {
                "samples": ["6", 0],
                "vae": ["1", 2]
            },
            "class_type": "VAEDecode"
        },
        
        # === SAVE VIDEO ===
        "8": {
            "inputs": {
                "filename_prefix": f"lookbook_{job_id}",
                "images": ["7", 0],
                "fps": 25,
                "format": "video/h264-mp4"
            },
            "class_type": "SaveAnimatedWEBP"  # o VHS_VideoCombine si disponible
        }
    }
    
    # Enviar workflow de video a ComfyUI
    # ComfyUI AUTOMÁTICAMENTE descargará Klein de VRAM y cargará LTX-2.3
    payload = {"prompt": video_workflow, "client_id": WORKER_ID}
    
    print(f"📤 [Job {job_id}] Enviando workflow LTX-2.3 a ComfyUI...")
    print(f"   LTX-2.3 ya cargado en VRAM (96GB --highvram) → inferencia directa")
    
    resp = requests.post(f"{COMFY_URL}/prompt", json=payload, timeout=30)
    resp.raise_for_status()
    
    prompt_id = resp.json()['prompt_id']
    print(f"📤 [Job {job_id}] Video prompt_id: {prompt_id}")
    
    update_job_progress(job_id, 65, "Procesando video en GPU...")
    
    # Esperar resultado del video (más tiempo que imagen)
    result_path = wait_for_comfy_result(
        job_id, prompt_id, '8',
        max_wait=300,        # 5 min máx para video
        total_steps=8        # 8 steps del sampler
    )
    
    print(f"✅ [Job {job_id}] Video generado: {result_path}")
    
    # Subir video a Supabase Storage
    update_job_progress(job_id, 88, "Subiendo video...")
    
    with open(result_path, 'rb') as f:
        video_data = f.read()
    
    video_filename = f"lookbook_{user_id}_{job_id}_{int(time.time())}.mp4"
    storage_path = f"{user_id}/videos/{video_filename}"
    
    supabase.storage.from_("avatars").upload(
        storage_path,
        video_data,
        file_options={"content-type": "video/mp4", "upsert": False}
    )
    
    public_url = supabase.storage.from_("avatars").get_public_url(storage_path)
    if isinstance(public_url, dict):
        public_url = public_url.get('publicUrl') or public_url.get('publicURL') or str(public_url)
    
    print(f"✅ [Job {job_id}] Video subido: {public_url[:80]}...")
    
    # Limpiar archivos temporales
    try:
        os.remove(video_input_path)
        os.remove(result_path)
    except:
        pass
    
    return public_url
```

---

### FASE 5: Worker — Nuevo flujo `process_job()` con imagen + video

#### 5.1 Modificar `process_job()` para try-on con video

```python
def process_job(job):
    """Procesar un job completo — ahora con video para try-on"""
    
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
        
        job_type = job.get('job_type', 'tryon')
        
        if job_type == 'face_enhancement':
            result_path = execute_face_enhancement(job)
        elif job_type == 'avatar_generation':
            result_path = execute_avatar_generation(job)
        else:
            # ========================================
            # TRY-ON: Imagen + Video
            # ========================================
            
            # PASO 1: Generar imagen try-on
            if UNET_CONFIG.get('model_type') == 'klein' and UNET_CONFIG.get('has_tryon_lora'):
                result_path = execute_klein_tryon(job)
            else:
                result_path = execute_flux_direct(job)  # Fallback a Kontext
            
            # Subir imagen a Storage
            tryon_image_url = upload_result_to_supabase(job_id, user_id, result_path)
            
            # ========================================
            # ENVIAR IMAGEN INMEDIATAMENTE A LA APP
            # (progreso 55% — la app ya puede mostrar el look)
            # ========================================
            supabase.table('ai_generation_jobs').update({
                'progress': 55,
                'result_url': tryon_image_url,
                'result_metadata': {
                    'worker_id': WORKER_ID,
                    'backend': 'vast',
                    'tryon_image_url': tryon_image_url,
                    'video_status': 'generating',
                    'status_message': 'Look generado! Generando video lookbook...'
                }
            }).eq('id', job_id).execute()
            
            # Insertar en tryon_results CON imagen (sin video todavía)
            products_metadata = job['input_data'].get('products_metadata', [])
            tryon_insert = supabase.table('tryon_results').insert({
                'user_id': user_id,
                'job_id': job_id,
                'result_url': tryon_image_url,
                'products_used': products_metadata,
                'video_status': 'generating',
            }).execute()
            
            tryon_result_id = tryon_insert.data[0]['id'] if tryon_insert.data else None
            
            print(f"📸 [Job {job_id}] Imagen enviada a app, generando video...")
            
            # ========================================
            # PASO 2: Generar video lookbook LOCAL (si falla no rompe)
            # ComfyUI hará swap automático de VRAM: Klein → LTX-2.3
            # ========================================
            video_url = None
            try:
                # Verificar que LTX-2.3 está disponible
                ltx_available = (
                    os.path.exists("/workspace/ComfyUI/models/checkpoints/ltx-2.3-distilled.safetensors") or
                    os.path.exists("/workspace/ComfyUI/models/checkpoints/ltx-2.3-dev.safetensors")
                )
                
                if ltx_available:
                    video_url = generate_lookbook_video(
                        job_id, result_path, user_id, products_metadata
                    )
                    
                    # Actualizar tryon_results con video
                    if tryon_result_id:
                        supabase.table('tryon_results').update({
                            'video_url': video_url,
                            'video_status': 'completed',
                        }).eq('id', tryon_result_id).execute()
                    
                    print(f"🎬 [Job {job_id}] Video lookbook listo: {video_url[:60]}...")
                else:
                    print(f"⚠️ [Job {job_id}] LTX-2.3 no disponible, skip video")
                    if tryon_result_id:
                        supabase.table('tryon_results').update({
                            'video_status': 'skipped',
                        }).eq('id', tryon_result_id).execute()
                        
            except Exception as video_err:
                print(f"⚠️ [Job {job_id}] Video falló (imagen ya entregada): {video_err}")
                if tryon_result_id:
                    supabase.table('tryon_results').update({
                        'video_status': 'failed',
                    }).eq('id', tryon_result_id).execute()
            
            # ========================================
            # COMPLETAR JOB
            # ========================================
            processing_time = time.time() - start_time
            
            final_metadata = {
                'worker_id': WORKER_ID,
                'backend': 'vast',
                'tryon_image_url': tryon_image_url,
                'video_url': video_url,
                'video_status': 'completed' if video_url else 'failed',
                'status_message': 'Look y video listos!' if video_url else 'Look generado (video no disponible)',
            }
            
            supabase.table('ai_generation_jobs').update({
                'status': 'completed',
                'progress': 100,
                'result_url': tryon_image_url,
                'completed_at': datetime.utcnow().isoformat(),
                'processing_time_seconds': round(processing_time, 2),
                'cost_usd': 0.01 if video_url else 0.005,  # Todo local, solo GPU time
                'result_metadata': final_metadata,
            }).eq('id', job_id).execute()
            
            print(f"✅ [Job {job_id}] Completado en {processing_time:.1f}s")
            
            # Limpiar archivos temporales
            try:
                os.remove(result_path)
            except:
                pass
            
            return True
        
        # ========================================
        # Para face_enhancement y avatar_generation (sin cambios)
        # ========================================
        supabase.table('ai_generation_jobs').update({
            'progress': 90
        }).eq('id', job_id).execute()
        
        public_url = upload_result_to_supabase(job_id, user_id, result_path)
        
        # ... (resto del código actual para face/avatar sin cambios) ...
        
        if job_type == 'face_enhancement':
            supabase.table('profiles').update({
                'face_hd_url': public_url,
                'face_enhanced_at': datetime.utcnow().isoformat()
            }).eq('id', user_id).execute()
            
            if job.get('input_data', {}).get('auto_generate_avatar'):
                try:
                    profile_resp = supabase.table('profiles').select('gender, height_cm').eq('id', user_id).single().execute()
                    profile_data = profile_resp.data if profile_resp.data else {}
                    
                    supabase.table('ai_generation_jobs').insert({
                        'user_id': user_id,
                        'job_type': 'avatar_generation',
                        'status': 'pending',
                        'preferred_backend': 'vast',
                        'priority': 9,
                        'input_data': {
                            'face_hd_url': public_url,
                            'gender': profile_data.get('gender') or job['input_data'].get('gender'),
                            'body_analysis': job['input_data'].get('body_analysis', {}),
                            'height_cm': profile_data.get('height_cm') or job['input_data'].get('height_cm', 170),
                        },
                    }).execute()
                except Exception as auto_err:
                    print(f"⚠️ [Job {job_id}] Error auto-encolando avatar: {auto_err}")
                    
        elif job_type == 'avatar_generation':
            existing = supabase.table('virtual_avatars').select('id').eq('user_id', user_id).execute()
            
            if existing.data and len(existing.data) > 0:
                supabase.table('virtual_avatars').update({
                    'base_avatar_url': public_url,
                    'base_avatar_generated_at': datetime.utcnow().isoformat(),
                    'base_avatar_status': 'completed',
                    'updated_at': datetime.utcnow().isoformat()
                }).eq('user_id', user_id).execute()
            else:
                supabase.table('virtual_avatars').insert({
                    'user_id': user_id,
                    'base_avatar_url': public_url,
                    'base_avatar_generated_at': datetime.utcnow().isoformat(),
                    'base_avatar_status': 'completed'
                }).execute()
            
            supabase.table('profiles').update({
                'avatar_url': public_url,
                'avatar_generated_at': datetime.utcnow().isoformat()
            }).eq('id', user_id).execute()
        
        processing_time = time.time() - start_time
        supabase.table('ai_generation_jobs').update({
            'status': 'completed',
            'progress': 100,
            'result_url': public_url,
            'completed_at': datetime.utcnow().isoformat(),
            'processing_time_seconds': round(processing_time, 2),
            'cost_usd': 0.005,
        }).eq('id', job_id).execute()
        
        try:
            os.remove(result_path)
        except:
            pass
        
        return True
        
    except Exception as e:
        print(f"❌ [Job {job_id}] Error: {e}")
        supabase.table('ai_generation_jobs').update({
            'status': 'failed',
            'error_message': str(e),
            'completed_at': datetime.utcnow().isoformat(),
        }).eq('id', job_id).execute()
        return False
```

---

### FASE 6: App — Recibir imagen + video

#### 6.1 La app ya recibe updates vía Realtime

El flujo de updates que recibirá la app:

```
Evento 1 (progress=10):  "Procesando..."
Evento 2 (progress=20):  "Generando look con Klein LoRA..."
Evento 3 (progress=55):  "Look generado! Generando video lookbook..."
                          → result_url = imagen try-on ✅
                          → result_metadata.video_status = "generating"
Evento 4 (progress=85):  "Subiendo video..."
Evento 5 (progress=100): "Look y video listos!"
                          → result_metadata.video_url = video URL ✅
                          → result_metadata.video_status = "completed"
```

#### 6.2 Cambios en `tryon.tsx`

En el handler de Realtime que recibe job updates, detectar:
1. Cuando `progress >= 55` y `result_url` existe → mostrar imagen del look
2. Cuando `result_metadata.video_status === 'generating'` → mostrar spinner "Generando video..."
3. Cuando `result_metadata.video_status === 'completed'` → mostrar reproductor de video

#### 6.3 Cambios en `tryon_results` display

Cuando se muestran resultados guardados:
1. Si `video_url` existe → mostrar botón de play sobre la imagen
2. Si `video_status === 'generating'` → mostrar indicador
3. Si `video_status === 'failed'` → solo mostrar imagen (sin video)

#### 6.4 Posts con video

Al publicar un look como post:
- Si tiene `video_url` → `media_type = 'video'`, `media_url = video_url`, `media_urls = [image_url, video_url]`
- Si solo imagen → como actualmente

---

### FASE 7: Try-Off para Wardrobe (Feature secundaria)

#### 7.1 Nuevo job_type: `tryoff`

```python
def execute_tryoff(job):
    """
    Extraer prenda de una foto del usuario
    Input: foto de persona vistiendo ropa
    Output: imagen de la prenda aislada sobre fondo blanco
    """
    job_id = job['id']
    
    COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
    Path(COMFY_INPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    # Descargar foto
    photo_url = job['input_data']['photo_url']
    photo_filename = f"tryoff_{job_id}.jpg"
    photo_path = f"{COMFY_INPUT_DIR}/{photo_filename}"
    download_image(photo_url, photo_path)
    
    garment_type = job['input_data'].get('garment_type', 'outfit')
    
    prompt = f"TRYOFF extract the {garment_type} over a white background, product photography style. NO HUMAN VISIBLE (the garments maintain their 3D form like an invisible mannequin)."
    
    # Workflow similar a Klein try-on pero con LoRA try-off
    # y solo 1 imagen de input
    workflow = {
        "12": {
            "inputs": {
                "unet_name": UNET_CONFIG["name"],
                "weight_dtype": UNET_CONFIG["dtype"]
            },
            "class_type": "UNETLoader"
        },
        "70": {
            "inputs": {
                "lora_name": "virtual-tryoff-lora_comfy.safetensors",
                "strength_model": 1.0,
                "model": ["12", 0]
            },
            "class_type": "LoraLoaderModelOnly"
        },
        # ... (resto del workflow similar, 1 imagen input) ...
    }
    
    # ... (enviar a ComfyUI, esperar resultado) ...
```

#### 7.2 Uso en Wardrobe

Cuando un usuario sube una foto al wardrobe:
1. Detectar prendas en la foto (ya existe categorización)
2. Crear job `tryoff` para extraer cada prenda
3. Guardar imágenes limpias de productos en wardrobe_items

---

## 📊 RESUMEN DE CAMBIOS

### Archivos a modificar

| Archivo | Cambio | Riesgo |
|---------|--------|--------|
| `provision-looks.sh` | Swap modelo FLUX dev → Klein 9B + descargar LoRAs + LTX-2.3 + custom nodes | ⚠️ Medio (requiere re-provision) |
| `worker_vast.py` | Añadir `execute_klein_tryon()`, `generate_lookbook_video()`, modificar `process_job()` | ⚠️ Medio |
| `requirements.txt` | Sin cambios (todo via ComfyUI) | ✅ Zero |
| BD `tryon_results` | Añadir campos `video_url`, `video_status` | ⬇️ Bajo |
| App `tryon.tsx` | Detectar video en updates, mostrar reproductor | ⬇️ Bajo |
| App `avatars-realtime.ts` | Sin cambios (ya soporta metadata) | ✅ Zero |

### Variables de entorno nuevas en Vast

**Ninguna.** Todo corre local. Las mismas `SUPABASE_URL`, `SUPABASE_KEY`, `WORKER_ID` que ya usamos.

### Modelos descargados (nuevo provisioning)

| Modelo | Tamaño | Ubicación |
|--------|--------|-----------|
| `flux2-klein-9b.safetensors` | ~18GB | `/workspace/ComfyUI/models/diffusion_models/` |
| `flux-klein-tryon-comfy.safetensors` | ~500MB | `/workspace/ComfyUI/models/loras/` |
| `virtual-tryoff-lora_comfy.safetensors` | ~500MB | `/workspace/ComfyUI/models/loras/` |
| `ltx-2.3-distilled.safetensors` | ~10-15GB | `/workspace/ComfyUI/models/checkpoints/` |
| `t5xxl_fp8_e4m3fn.safetensors` | ~5GB | `/workspace/ComfyUI/models/text_encoders/` |
| ~~`flux2-dev-nvfp4.safetensors`~~ | ~~12GB~~ | ~~ELIMINADO~~ |

**Espacio disco total nuevo**: ~45-55GB (vs actual ~20GB). Cabe sobradamente en disco Vast.

### Costos por generación (TODO LOCAL, 96GB, SIN SWAP)

| Operación | Costo | Tiempo |
|-----------|-------|--------|
| Try-on imagen (Klein bf16 local) | ~$0.005 (GPU Vast) | ~10-15s |
| Video lookbook (LTX-2.3 bf16 local) | ~$0.008 (GPU Vast) | ~20-40s |
| **Total try-on + video** | **~$0.013** | **~30-55s** |
| Try-off (Klein local) | ~$0.003 | ~8s |

**Sin swap = sin latencia muerta.** Klein genera imagen → LTX genera video
inmediatamente porque ambos ya están cargados en los 96GB.

**Comparativa**: Antes (solo imagen FLUX Kontext): ~$0.005 / ~24s
**Ahora**: Imagen + Video 5s: ~$0.013 / ~45s → **Video gratis en términos prácticos**

### Tiempos para el usuario (96GB, sin swap)

| Evento | Tiempo aprox | Lo que ve el usuario |
|--------|-------------|---------------------|
| Job creado | 0s | "Procesando..." |
| Klein genera imagen | ~12s | Progreso 15-50% |
| **Imagen try-on lista** | **~15s** | 📸 **Ve su look con las prendas** |
| LTX genera video | ~35s | "Generando video lookbook..." |
| **Video listo** | **~45s total** | 🎬 **Ve video 5s con múltiples ángulos** |

**Clave UX**: El usuario ve la IMAGEN a los ~15s. El video aparece ~30s después.
Sin swap de modelos = el video empieza a generarse INMEDIATAMENTE después de la imagen.

---

## 🔄 DIAGRAMA DE FLUJO COMPLETO

```
┌─────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  APP    │────▶│  Supabase        │────▶│  VAST WORKER        │
│ tryon   │     │  ai_generation   │     │  worker_vast.py     │
│ .tsx    │     │  _jobs           │     │                     │
└─────────┘     └──────────────────┘     └──────────┬──────────┘
     ▲                   ▲                          │
     │                   │                          ▼
     │                   │              ┌───────────────────────┐
     │                   │              │  PASO 1: Klein Try-On │
     │                   │              │  (ComfyUI local)      │
     │                   │              │  FLUX Klein 9B + LoRA │
     │                   │              │  ~15-20 segundos      │
     │                   │              └───────────┬───────────┘
     │                   │                          │
     │         ┌─────────┴──────────┐               │
     │         │ UPDATE progress=55 │◀──────────────┘
     │         │ result_url=IMAGEN  │  ← App muestra look
     │         │ video_status=      │
     │         │   "generating"     │
     │         └─────────┬──────────┘
     │                   │                          │
     │                   │              ┌───────────▼───────────┐
     │                   │              │  PASO 2: LTX-2.3     │
     │                   │              │  (ComfyUI LOCAL)      │
     │                   │              │  YA CARGADO en VRAM   │
     │                   │              │  Image → Video 5s     │
     │                   │              │  ~20-30 segundos      │
     │                   │              └───────────┬───────────┘
     │                   │                          │
     │         ┌─────────┴──────────┐               │
     │         │ UPDATE progress=100│◀──────────────┘
     │         │ video_url=VIDEO    │  ← App muestra video
     │         │ video_status=      │
     │         │   "completed"      │
     │         └────────────────────┘
     │
     │  ← Supabase Realtime (subscribeToUserJobs)
     │     La app recibe CADA update automáticamente
```

---

## ⚠️ RIESGOS Y MITIGACIONES

| Riesgo | Probabilidad | Mitigación |
|--------|-------------|------------|
| ComfyUI no soporta Klein Edit + LoRA | Baja | El LoRA tiene versión comfy explícita; fallback a Kontext |
| LTX-2.3 nodos ComfyUI incompatibles | Media | Verificar ComfyUI-LTXVideo antes de deploy; fallback sin video |
| Video quality baja con LTX-2.3 | Media | Ajustar prompt; usar versión dev en vez de distilled si calidad baja |
| Provisioning tarda con modelos extra | Media | Klein reemplaza dev + LTX ~15GB extra; total ~30 min provision |
| LoRA Klein solo soporta top+bottom | Baja | Para >2 prendas, fallback a execute_flux_direct() |
| Disco insuficiente en Vast | Baja | Pedir ≥100GB disco; modelos totales ~50GB |
| VRAM insuficiente | ❌ NO | 96GB = 43GB usados + 53GB libres. Sobra para todo |

---

## 📋 ORDEN DE IMPLEMENTACIÓN

1. **[ ] Migración BD**: `ALTER TABLE tryon_results ADD video_url TEXT, ADD video_status TEXT`
2. **[ ] Provisioning**: Actualizar `provision-looks.sh`:
   - Swap FLUX dev → Klein 9B
   - Descargar LoRAs (try-on + try-off)
   - Descargar LTX-2.3 (distilled o dev)
   - Descargar T5-XXL fp8 para LTX
   - Instalar ComfyUI-LTXVideo custom nodes
3. **[ ] Worker**: Actualizar `get_optimal_unet_config()` para detectar Klein
4. **[ ] Worker**: Implementar `execute_klein_tryon()` (workflow ComfyUI Klein+LoRA)
5. **[ ] Worker**: Implementar `build_lookbook_video_prompt()` (prompt dinámico)
6. **[ ] Worker**: Implementar `generate_lookbook_video()` (workflow ComfyUI LTX-2.3)
7. **[ ] Worker**: Modificar `process_job()` con flujo imagen→video secuencial
8. **[ ] Worker**: Implementar `wait_for_comfy_video_result()` (videos en vez de imágenes)
9. **[ ] App**: Modificar tryon.tsx para mostrar video cuando llegue vía Realtime
10. **[ ] App**: Actualizar publicación de posts con soporte video
11. **[ ] Provisioning**: Asegurar `--highvram` en arranque de ComfyUI (96GB)
12. **[ ] Test**: Probar en Vast con instancia RTX 6000 96GB, ≥100GB disco
13. **[ ] Deploy**: Push a repo `vestuario-vast-worker`, crear nueva instancia

---

## 🎯 RESULTADO FINAL

El usuario de LOOKS experimentará:

1. **Selecciona prendas** en la pantalla Try-On
2. **Pulsa "Probar"**
3. **~20 segundos**: Ve su imagen con las prendas puestas 📸
4. **Indicador**: "Generando video lookbook..."
5. **~50 segundos**: Ve un video profesional de 5 segundos con múltiples ángulos 🎬
   - Shot 1: Frontal completo (su cara visible)
   - Shots 2-5: Zooms detallados en cada prenda (sin cara → no pierde referencia)
6. **Puede publicar** el look como post con imagen O video

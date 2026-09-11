"""AI image generator for video scenes - modular provider system.

Supports multiple providers (fal.ai, Leonardo.ai, etc.) with a unified interface.
Add new providers by implementing the BaseImageGenerator interface.

Usage:
    generator = get_image_generator("fal")
    image_path = generator.generate("A futuristic city", Path("/tmp/image"))
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import requests

from .config import env
from .utils import log


# ── Base Interface ────────────────────────────────────────────────────────────

class BaseImageGenerator(ABC):
    """Abstract base class for image generation providers.
    
    To add a new provider:
    1. Create a class that inherits from BaseImageGenerator
    2. Implement the generate() method
    3. Register it with @register_generator
    """
    
    @abstractmethod
    def generate(
        self,
        prompt: str,
        dest: Path,
        style: str = "cinematic, high detail, dramatic lighting, vertical 9:16",
    ) -> Path | None:
        """Generate an image from a text prompt."""
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging."""
        pass
    
    @property
    def is_available(self) -> bool:
        """Check if this provider is properly configured."""
        return True


# ── Provider Registry ────────────────────────────────────────────────────────

_generators: dict[str, type[BaseImageGenerator]] = {}


def register_generator(cls: type[BaseImageGenerator]) -> type[BaseImageGenerator]:
    """Decorator to register an image generator provider."""
    _generators[cls().name] = cls
    return cls


def get_image_generator(provider: str = "auto") -> BaseImageGenerator:
    """Get an image generator instance."""
    if provider == "auto":
        for name in ("fal", "leonardo"):
            if name in _generators:
                gen = _generators[name]()
                if gen.is_available:
                    return gen
        for name, cls in _generators.items():
            gen = cls()
            if gen.is_available:
                return gen
        raise RuntimeError("No image generator provider available")
    
    if provider not in _generators:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(_generators.keys())}")
    
    return _generators[provider]()


def list_generators() -> list[str]:
    """List available generator names."""
    return [name for name, cls in _generators.items() if cls().is_available]


# ── fal.ai Provider ──────────────────────────────────────────────────────────

@register_generator
class FalImageGenerator(BaseImageGenerator):
    """Image generator using fal.ai API. Free tier at https://fal.ai. Set FAL_KEY in .env."""
    
    def __init__(self, api_key: str | None = None, model: str = "fal-ai/flux/schnell"):
        self._api_key = api_key or env("FAL_KEY")
        self._model = model
    
    @property
    def name(self) -> str:
        return "fal"
    
    @property
    def is_available(self) -> bool:
        return bool(self._api_key)
    
    def generate(self, prompt: str, dest: Path, style: str = "cinematic, high detail, dramatic lighting, vertical 9:16") -> Path | None:
        if not self.is_available:
            return None
        
        full_prompt = f"{prompt}, {style}"
        out = dest.with_suffix(".png")
        
        try:
            r = requests.post(
                f"https://fal.run/{self._model}",
                headers={"Authorization": f"Key {self._api_key}", "Content-Type": "application/json"},
                json={"prompt": full_prompt, "image_size": "portrait_16_9"},
                timeout=60,
            )
            r.raise_for_status()
            img_url = r.json()["images"][0]["url"]
            
            img_r = requests.get(img_url, timeout=30)
            img_r.raise_for_status()
            out.write_bytes(img_r.content)
            
            if out.exists() and out.stat().st_size > 1000:
                log(f"fal.ai: generated {out.name} ({out.stat().st_size / 1024:.0f}KB)", "ok")
                return out
            return None
        except Exception as e:
            log(f"fal.ai: {e}", "warn")
            return None


# ── Leonardo.ai Provider ─────────────────────────────────────────────────────

@register_generator
class LeonardoImageGenerator(BaseImageGenerator):
    """Image generator using Leonardo.ai API. 150 credits/day free. Set LEONARDO_API_KEY in .env."""
    
    def __init__(self, api_key: str | None = None, model: str = "6b645e3a-36a0-4e4a-86f2-0cfb94a1d1e4"):
        self._api_key = api_key or env("LEONARDO_API_KEY")
        self._model = model
    
    @property
    def name(self) -> str:
        return "leonardo"
    
    @property
    def is_available(self) -> bool:
        return bool(self._api_key)
    
    def generate(self, prompt: str, dest: Path, style: str = "cinematic, high detail, dramatic lighting, vertical 9:16") -> Path | None:
        if not self.is_available:
            return None
        
        full_prompt = f"{prompt}, {style}"
        out = dest.with_suffix(".png")
        
        try:
            r = requests.post(
                "https://cloud.leonardo.ai/api/rest/v1/generations",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"prompt": full_prompt, "modelId": self._model, "width": 1024, "height": 1920, "num_images": 1},
                timeout=30,
            )
            r.raise_for_status()
            gen_id = r.json()["sdGenerationJob"]["generationId"]
            
            for _ in range(30):
                time.sleep(2)
                r = requests.get(
                    f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=15,
                )
                r.raise_for_status()
                status = r.json().get("generations_by_pk", {}).get("status", "")
                if status == "COMPLETE":
                    img_url = r.json()["generations_by_pk"]["generated_images"][0]["url"]
                    break
                elif status == "FAILED":
                    return None
            else:
                return None
            
            img_r = requests.get(img_url, timeout=30)
            img_r.raise_for_status()
            out.write_bytes(img_r.content)
            
            if out.exists() and out.stat().st_size > 1000:
                log(f"Leonardo: generated {out.name} ({out.stat().st_size / 1024:.0f}KB)", "ok")
                return out
            return None
        except Exception as e:
            log(f"Leonardo: {e}", "warn")
            return None


# ── Unified Interface ────────────────────────────────────────────────────────

def generate_scene_image(scene_description: str, dest: Path, style: str = "cinematic, high detail, dramatic lighting, vertical 9:16", provider: str = "auto") -> Path | None:
    """Generate an image for a video scene. Main entry point."""
    if provider == "auto":
        for name in ("fal", "leonardo"):
            try:
                gen = get_image_generator(name)
                result = gen.generate(scene_description, dest, style)
                if result:
                    return result
            except Exception as e:
                log(f"Provider {name} failed: {e}", "warn")
                continue
        return None
    
    try:
        gen = get_image_generator(provider)
        return gen.generate(scene_description, dest, style)
    except Exception as e:
        log(f"Image generation failed: {e}", "warn")
        return None


# ── Backwards Compatibility ──────────────────────────────────────────────────

def generate_image_fal(prompt: str, dest: Path, **kwargs) -> Path | None:
    """Legacy function for backwards compatibility."""
    return FalImageGenerator().generate(prompt, dest, **kwargs)


def generate_image_leonardo(prompt: str, dest: Path, **kwargs) -> Path | None:
    """Legacy function for backwards compatibility."""
    return LeonardoImageGenerator().generate(prompt, dest, **kwargs)
"""Busca inteligente que prioriza conteúdo oficial de marcas/jogos/entidades específicas"""
from __future__ import annotations
import re
from typing import List, Tuple

# Base de conhecimento sobre entidades populares
KNOWN_ENTITIES = {
    "jogos": [
        "gta", "grand theft auto", "gta 6", "gta v", "gta vice city", "gta san andreas",
        "red dead redemption", "red dead", "call of duty", "cod", "warzone", "minecraft",
        "fortnite", "valorant", "counter strike", "cs:go", "apex legends", "league of legends",
        "world of warcraft", "overwatch", "cyberpunk", "elden ring", "zelda", "mario",
        "pokemon", "rockstar", "rockstar games", "sony", "playstation", "ps5", "xbox",
        "bethesda", "ubisoft", "activision", "blizzard", "ea", "electronic arts"
    ],
    "marcas": [
        "iphone", "apple", "samsung", "google", "tesla", "tesla model", "elon musk",
        "microsoft", "windows", "macbook", "nike", "adidas", "coca-cola", "amazon",
        "netflix", "disney", "marvel", "dc", "star wars", "star trek", "pokemon",
        "facebook", "meta", "instagram", "youtube", "tiktok"
    ]
}

# Termos que indicam conteúdo oficial/autêntico
OFFICIAL_INDICATORS = [
    "official", "gameplay", "screenshot", "trailer", "reveal",
    "announcement", "launch", "release", "developer", "behind the scenes",
    "logo", "art", "concept art", "character", "map"
]


def extract_named_entities(text: str) -> List[Tuple[str, str]]:
    """Extrai entidades nomeadas do texto usando padrões regex.
    
    Retorna lista de tuplas (entidade, tipo) onde tipo é 'game', 'brand', 'person' ou 'general'
    """
    text_lower = text.lower()
    entities = []
    
    # Extrair nomes próprios - padrão: palavras com inicial maiúscula sequenciais
    # Ex: "GTA 6", "Rockstar Games", "Elon Musk"
    proper_noun_pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b'
    proper_nouns = re.findall(proper_noun_pattern, text)
    
    # Verificar contra base de conhecimento
    for entity in KNOWN_ENTITIES.get("jogos", []):
        if entity in text_lower:
            entities.append((entity, "game"))
    
    for entity in KNOWN_ENTITIES.get("marcas", []):
        if entity in text_lower:
            entities.append((entity, "brand"))
    
    # Adicionar nouns próprios encontrados
    for noun in proper_nouns:
        noun_lower = noun.lower()
        if noun_lower not in [e[0] for e in entities]:
            # Tentar identificar se é pessoa (Musk, Taylor Swift, etc.)
            if any(indicator in noun_lower for indicator in ["CEO", "Founder", "Actor", "Singer"]):
                entities.append((noun, "person"))
            else:
                entities.append((noun, "general"))
    
    # Remover duplicatas mantendo ordem
    seen = set()
    unique = []
    for entity, type_ in entities:
        if entity not in seen:
            seen.add(entity)
            unique.append((entity, type_))
    
    return unique


def build_official_search_queries(entities: List[Tuple[str, str]], topic_words: List[str]) -> List[str]:
    """Constrói queries de busca priorizando conteúdo oficial.
    
    Estratégia:
    1. Combinar entidades com termos oficiais (ex: "GTA 6 official")
    2. Combinar entidades com contexto visual (ex: "GTA 6 gameplay screenshot")
    3. Usar palavras do tópico como fallback
    """
    queries = []
    
    # 1. Priorizar conteúdo oficial das entidades identificadas
    for entity, type_ in entities[:3]:  # Top 3 entidades
        for indicator in OFFICIAL_INDICATORS[:4]:  # Top 4 indicadores
            query = f"{entity} {indicator}"
            if query not in queries:
                queries.append(query)
    
    # 2. Combinar com palavras do tópico
    if topic_words:
        for entity, _ in entities[:2]:
            for word in topic_words[:3]:
                query = f"{entity} {word}"
                if query not in queries:
                    queries.append(query)
    
    # 3. Fallback genérico - usar palavras do tópico
    if topic_words:
        topic_query = " ".join(topic_words[:4])
        if topic_query not in queries:
            queries.append(topic_query)
    
    # Garantir pelo menos 3 queries
    while len(queries) < 3:
        if entities:
            queries.append(entities[0][0])
        else:
            queries.append("b-roll footage")
    
    return queries[:6]  # Limitar a 6 queries


def smart_query_enrichment(original_query: str, script_text: str, topic_words: List[str]) -> List[str]:
    """Enriquece uma query original com buscas por conteúdo oficial.
    
    Recebe a query original do roteiro e retorna lista de queries enriquecidas
    priorizando conteúdo oficial da marca/jogo mencionado.
    """
    # Extrair entidades do texto completo
    entities = extract_named_entities(script_text)
    
    if not entities:
        # Sem entidades identificadas, usar query original
        return [original_query]
    
    # Construir queries enriquecidas
    enriched = build_official_search_queries(entities, topic_words)
    
    # Garantir que a query original esteja inclusa (como fallback)
    if original_query not in enriched:
        enriched.append(original_query)
    
    return enriched
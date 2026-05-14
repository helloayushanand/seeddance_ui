import base64
import re
from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

# ── Prompt examples (few-shot) ────────────────────────────────────────────────
PROMPT_EXAMPLES = [
    """Product: Huda Beauty Liquid Matte "Trendsetter"
Core Visual: A cinematic, fast-paced 15-second commercial.
Opening (0-3s): Sleek macro close-up of lipstick tube floating in 3D iridescent space. Camera zooms out rapidly as black cap "pops" off in sync with heavy bass drop, revealing a swirl of liquid terracotta pigment morphing into the product name in bold liquid-metal font.
Middle (3-10s): Rapid-fire match-cut transitions. Cut 1: Gen-Z model with glowing skin applies lipstick in blurry lo-fi handheld style. Cut 2: Scene shifts to glitchy Y2K-inspired digital aesthetic with neon trail effects. Cut 3: Extreme macro of liquid texture drying from high-shine gloss to soft velvety matte in real-time.
Closing (10-15s): Product slams onto reflective glass surface, sending shockwave of matte powder across screen. Background shifts to minimalist vibrant sunset orange. Text overlay: "THE NEW MATTE" in high-contrast typography.
Style: 16mm film grain, high-shutter speed motion blur, ultra-realistic textures, vibrant color grading (terracotta, deep black, iridescent highlights), fast rhythmic editing, stop-motion vibes mixed with fluid 3D animation.""",

    """Product: Nike Air Max campaign
Opening (0-4s): Extreme close-up of shoe sole hitting wet pavement in slow motion, water droplets exploding outward in 4K. Camera pulls back in one continuous shot revealing a runner in neon-lit urban tunnel.
Middle (4-11s): Split-screen sequence — left side shows classic 1987 Air Max, right side morphs in real-time to current model. Transition using a whoosh of compressed air particles. Cut to overhead drone shot of runner crossing empty city bridge at golden hour.
Closing (11-15s): Shoe levitates center frame against clean white void. Air bubble visible through translucent sole pulses like a heartbeat. Tagline materializes letter by letter.
Style: Hyper-realistic CGI blended with live action, anamorphic lens flares, desaturated urban palette with pops of neon orange, rhythmic bass-driven editing pace."""
]

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are an expert AI video prompt engineer specializing in Seedance 2.0, BytePlus's state-of-the-art video generation model.

Your job: help users craft detailed, cinematic, effective video generation prompts through natural conversation. Users may be marketers, designers, or product teams — they know their vision but not the technical language.

## How a great Seedance prompt is structured:
1. **Opening (0-Xs)** — First impression: camera angle, subject intro, hook
2. **Middle sequence** — Key moments, transitions, camera movements, effects
3. **Closing (Xs-end)** — Final shot, reveal, text overlay if needed
4. **Style & Aesthetic** — Film style, color grade, texture, editing rhythm, mood

## What makes prompts work well in Seedance:
- Specific camera movements: "macro close-up", "drone pull-back", "handheld lo-fi"
- Concrete transitions: "match-cut", "morph", "whip pan", "smash cut"
- Texture/material descriptions: "velvety matte", "iridescent", "liquid metal"
- Timing cues: "(0-3s)", "(3-10s)" help structure multi-beat videos
- Style references: "16mm film grain", "Y2K aesthetic", "anamorphic lens flares"
- Emotion/energy: "fast rhythmic", "slow burn luxury", "kinetic energy"

## Your conversation style:
- Ask 1-2 focused questions per turn — never overwhelm
- Mirror the user's energy (excited brief messages → snappy replies; detailed messages → detailed response)
- If they upload an image, analyze it and suggest a video treatment before asking questions
- After 2-3 turns you should have enough to write a full prompt
- When you produce a final prompt, ALWAYS wrap it in <PROMPT> tags exactly like this:

<PROMPT>
[complete detailed prompt here]
</PROMPT>

- After outputting a prompt, ask "Want me to adjust anything — pacing, style, specific shots?"
- You can produce multiple iterations. Each refined version should also be in <PROMPT> tags.

## Example of excellent prompts:
{chr(10).join(f"Example {i+1}:{chr(10)}{ex}" for i, ex in enumerate(PROMPT_EXAMPLES))}

## Important:
- Never ask for information you can infer
- Translate vague words: "cool" → ask what kind of cool (minimal luxury? gritty street? futuristic?)
- If user says "like [brand/movie]", extract the visual language and apply it
- Always be enthusiastic — good prompts are creative work worth celebrating
"""

# ── Tools ─────────────────────────────────────────────────────────────────────
@tool
def get_prompt_examples() -> str:
    """Return high-quality example Seedance video prompts for reference and inspiration."""
    return "\n\n---\n\n".join(PROMPT_EXAMPLES)


@tool
def get_seedance_style_guide() -> str:
    """Return a guide of effective visual styles, camera techniques, and transitions that work well in Seedance 2.0."""
    return """
## Camera Movements
- Macro close-up → zoom out: great for product reveals
- Handheld lo-fi: authentic, Gen-Z, editorial feel
- Drone pull-back: scale, epic, landscape
- Dutch angle: tension, energy, attitude
- Whip pan: fast transitions, kinetic energy

## Transitions
- Match-cut: seamless story jumps
- Morph: liquid/organic transformations
- Smash cut: high contrast, dramatic
- Speed ramp: slow-mo into fast-mo
- Light burst: clean, premium

## Visual Styles
- 16mm film grain: vintage, authentic, fashion
- Anamorphic lens flares: cinematic, premium
- Y2K glitch: nostalgic, digital, trendy
- Minimal CGI void: luxury, product focus
- Golden hour live action: warm, lifestyle

## Color Approaches
- Terracotta + black + iridescent: beauty, editorial
- Neon + dark urban: tech, streetwear
- Desaturated + single color pop: premium, modern
- Warm analog: lifestyle, food, wellness

## Editing Rhythms
- Fast rhythmic cuts (bass-driven): energy products, youth
- Slow dissolves: luxury, fragrance, skincare
- Stop-motion hybrid: playful, creative, FMCG
- Single continuous shot: confidence, craftsmanship
"""


@tool
def analyze_prompt_quality(prompt: str) -> str:
    """Analyze a prompt draft and return specific improvement suggestions."""
    feedback = []
    if len(prompt) < 200:
        feedback.append("Prompt is short — add more specific camera movements and shot descriptions")
    if "opening" not in prompt.lower() and "0-" not in prompt:
        feedback.append("Missing timing structure — add time cues like (0-3s), (3-10s)")
    if not any(w in prompt.lower() for w in ["grain", "lens", "color", "grade", "style", "aesthetic"]):
        feedback.append("Missing style/aesthetic section — add film style and color grading details")
    if not any(w in prompt.lower() for w in ["cut", "transition", "morph", "pan", "zoom", "pull"]):
        feedback.append("Missing transition/movement language — specify how scenes connect")
    if not feedback:
        feedback.append("Prompt looks strong! Consider adding one more sensory detail (texture, sound design cue, or material description)")
    return "\n".join(f"• {f}" for f in feedback)


# ── Agent factory ─────────────────────────────────────────────────────────────
_memory = MemorySaver()
_agents: dict[str, object] = {}  # cache per api_key


def get_agent(gemini_api_key: str):
    if gemini_api_key not in _agents:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=gemini_api_key,
            temperature=0.8,
        )
        _agents[gemini_api_key] = create_react_agent(
            model=llm,
            tools=[get_prompt_examples, get_seedance_style_guide, analyze_prompt_quality],
            checkpointer=_memory,
            prompt=SYSTEM_PROMPT,
        )
    return _agents[gemini_api_key]


# ── Chat function ─────────────────────────────────────────────────────────────
def chat(gemini_api_key: str, user_message: str, image_bytes_list: list[bytes], thread_id: str) -> str:
    """Send a message (with optional images) and return the assistant's response."""
    print(f"[BOT] chat() called with message: {user_message[:50]}...")
    print(f"[BOT] thread_id: {thread_id}")
    print(f"[BOT] images: {len(image_bytes_list)} images")

    agent = get_agent(gemini_api_key)
    print(f"[BOT] agent created")

    content: list = [{"type": "text", "text": user_message}]
    for i, img_bytes in enumerate(image_bytes_list):
        try:
            b64 = base64.b64encode(img_bytes).decode()
            # Detect MIME type
            mime_type = "image/jpeg"
            if img_bytes[:8].startswith(b'\x89PNG'):
                mime_type = "image/png"
            elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
                mime_type = "image/webp"

            print(f"[BOT] loaded image {i}: {mime_type}")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"}
            })
        except Exception as e:
            print(f"[BOT] failed to load image {i}: {e}")

    print(f"[BOT] content prepared with {len(content)} items")

    config = {"configurable": {"thread_id": thread_id}}
    print(f"[BOT] invoking agent...")
    try:
        result = agent.invoke({"messages": [HumanMessage(content=content)]}, config=config)
        print(f"[BOT] agent returned result")
        resp = result["messages"][-1].content

        # Handle structured response (list of dicts) vs string response
        if isinstance(resp, list):
            print(f"[BOT] response is list, extracting text...")
            text_parts = []
            for item in resp:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            resp = "".join(text_parts)

        print(f"[BOT] response: {resp[:100]}...")
        return resp
    except Exception as e:
        print(f"[BOT] agent invoke failed: {e}")
        import traceback
        traceback.print_exc()
        raise


def get_history(gemini_api_key: str, thread_id: str) -> list[dict]:
    """Return conversation history as list of {role, content} dicts."""
    agent = get_agent(gemini_api_key)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = agent.get_state(config)
        messages = state.values.get("messages", [])
    except Exception:
        return []

    history = []
    for m in messages:
        if isinstance(m, HumanMessage):
            # Extract text only (strip image data for display)
            if isinstance(m.content, list):
                text = " ".join(p["text"] for p in m.content if isinstance(p, dict) and p.get("type") == "text")
            else:
                text = str(m.content)
            history.append({"role": "user", "content": text})
        elif isinstance(m, AIMessage):
            # Handle both string and list content
            if isinstance(m.content, str):
                if m.content.strip():
                    history.append({"role": "assistant", "content": m.content})
            elif isinstance(m.content, list):
                # Extract text from list of dicts
                text_parts = []
                for item in m.content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        text_parts.append(item)
                text = "".join(text_parts)
                if text.strip():
                    history.append({"role": "assistant", "content": text})
    return history


def extract_prompts(text: str) -> list[str]:
    """Extract all <PROMPT>...</PROMPT> blocks from a message."""
    return re.findall(r"<PROMPT>\s*(.*?)\s*</PROMPT>", text, re.DOTALL)

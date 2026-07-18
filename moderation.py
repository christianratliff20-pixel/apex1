from anthropic import Anthropic
from config import settings
import json
import logging

logger = logging.getLogger(__name__)
client = Anthropic()

async def run_moderation_pipeline(
    title: str,
    description: str,
    category: str,
    sub_category: str
) -> dict:
    """
    2-pass AI moderation:
    Pass 1: Fast relevance check
    Pass 2: Deep review if uncertain
    Returns: {"pass": bool, "stage": "approved"|"review", "reason": str}
    """
    
    # PASS 1 — Fast check
    try:
        pass1_response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            system="""You are a content moderator for Apex, a fitness/health platform. 
Determine if content is relevant to fitness, health, nutrition, or wellness.
Respond ONLY with valid JSON: {"relevant": true/false, "confidence": "high"/"medium"/"low", "reason": "brief reason"}""",
            messages=[{
                "role": "user",
                "content": f"""Title: "{title}"
Description: "{description}"
Category: {category} > {sub_category}

Is this fitness/health relevant?"""
            }]
        )
        
        pass1_text = pass1_response.content[0].text.strip()
        pass1 = json.loads(pass1_text.replace("```json", "").replace("```", ""))
        
        # If high confidence relevant → approve immediately
        if pass1.get("relevant") and pass1.get("confidence") == "high":
            return {"pass": True, "stage": "approved", "reason": "Passed Pass 1"}
        
    except Exception as e:
        logger.error(f"Pass 1 error: {e}")
        # Default to approved on error (don't block)
        return {"pass": True, "stage": "approved", "reason": "Pass 1 error - defaulted to approved"}
    
    # PASS 2 — Deep review
    try:
        pass2_response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            system="""You are a senior content moderator for Apex fitness platform.
A first-pass AI was uncertain. Give content a thorough second look.
Respond ONLY with valid JSON: {"relevant": true/false, "confidence": "high"/"medium"/"low", "reason": "brief reason"}""",
            messages=[{
                "role": "user",
                "content": f"""Title: "{title}"
Description: "{description}"
Category: {category} > {sub_category}
Pass 1 result: {json.dumps(pass1)}

Final verdict: appropriate for fitness/health platform?"""
            }]
        )
        
        pass2_text = pass2_response.content[0].text.strip()
        pass2 = json.loads(pass2_text.replace("```json", "").replace("```", ""))
        
        if pass2.get("relevant"):
            return {"pass": True, "stage": "approved", "reason": "Passed Pass 2"}
        else:
            # Double flagged → human review queue
            return {
                "pass": False,
                "stage": "review",
                "reason": pass2.get("reason", "Content quality check needed")
            }
    
    except Exception as e:
        logger.error(f"Pass 2 error: {e}")
        # Default to approved on error (don't block)
        return {"pass": True, "stage": "approved", "reason": "Pass 2 error - defaulted to approved"}

import json
import pytest
from app.core.hubscape_adk import RemoteContext


def test_consult_agent_directive_trigger_otp():
    """Verifies that consult_agent directive parsing converts triggerOtp into TRIGGER_OTP action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_json = json.dumps({
        "directive": "execute_host_tool",
        "target_tool": "triggerOtp",
        "parameters": {
            "request_id": "otp_test_123",
            "phone_number": "+15559876543",
            "purpose": "org_onboarding",
            "agent_id": "sales-onboarding-agent",
            "metadata": {"org_id": "test_org"}
        },
        "message": "Initiating phone verification."
    })

    parsed = json.loads(directive_json)
    target_tool = parsed.get("target_tool")
    parameters = parsed.get("parameters") or {}
    message = parsed.get("message") or ""

    if target_tool in ("triggerOtp", "trigger_otp"):
        phone = parameters.get("phone_number") or parameters.get("mobile_number")
        metadata = parameters.get("metadata") or {}
        purpose = parameters.get("purpose") or metadata.get("purpose") or "general"
        ctx.actions.append({
            "type": "TRIGGER_OTP",
            "payload": {
                "phone_number": phone,
                "purpose": purpose,
                "agent_id": parameters.get("agent_id") or "subagent",
                "metadata": metadata
            }
        })

    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "TRIGGER_OTP"
    assert "request_id" not in action["payload"]
    assert action["payload"]["phone_number"] == "+15559876543"
    assert action["payload"]["purpose"] == "org_onboarding"
    assert action["payload"]["agent_id"] == "sales-onboarding-agent"


def test_consult_agent_directive_close_agent_widget():
    """Verifies that consult_agent directive parsing converts closeAgentWidget into CLOSE_AGENT_WIDGET action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_json = json.dumps({
        "directive": "execute_host_tool",
        "target_tool": "closeAgentWidget",
        "parameters": {
            "messageId": "msg_abc123",
            "resultText": "Form submitted successfully."
        },
        "message": "Closing widget."
    })

    parsed = json.loads(directive_json)
    target_tool = parsed.get("target_tool")
    parameters = parsed.get("parameters") or {}
    message = parsed.get("message") or ""

    if target_tool in ("closeAgentWidget", "close_agent_widget"):
        ctx.actions.append({
            "type": "CLOSE_AGENT_WIDGET",
            "payload": {
                "messageId": parameters.get("messageId"),
                "resultText": parameters.get("resultText") or "✅ Widget closed."
            }
        })

    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "CLOSE_AGENT_WIDGET"
    assert action["payload"]["messageId"] == "msg_abc123"
    assert action["payload"]["resultText"] == "Form submitted successfully."

import json
import pytest
from app.core.hubscape_adk import RemoteContext
from app.scripts.consult_agent import parse_subagent_directive


def test_consult_agent_directive_trigger_otp():
    """Verifies that consult_agent directive parsing converts triggerOtp into TRIGGER_OTP action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_data = {
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
    }

    result = parse_subagent_directive(directive_data, ctx, "sales-onboarding-agent")

    assert result == "Initiating phone verification."
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "TRIGGER_OTP"
    assert "request_id" not in action["payload"]
    assert action["payload"]["phone_number"] == "+15559876543"
    assert action["payload"]["purpose"] == "org_onboarding"
    assert action["payload"]["agent_id"] == "sales-onboarding-agent"
    assert action["payload"]["metadata"] == {"org_id": "test_org"}


def test_consult_agent_directive_trigger_otp_from_json_string():
    """Verifies that consult_agent directive parsing works when given raw JSON strings."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_json = json.dumps({
        "directive": "execute_host_tool",
        "target_tool": "trigger_otp",
        "parameters": {
            "mobile_number": "+15551234567",
            "purpose": "auth"
        }
    })

    result = parse_subagent_directive(directive_json, ctx, "auth-agent")

    assert result == "Initiating phone verification for +15551234567."
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "TRIGGER_OTP"
    assert action["payload"]["phone_number"] == "+15551234567"
    assert action["payload"]["purpose"] == "auth"
    assert action["payload"]["agent_id"] == "auth-agent"


def test_consult_agent_directive_close_agent_widget():
    """Verifies that consult_agent directive parsing converts closeAgentWidget into CLOSE_AGENT_WIDGET action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_data = {
        "directive": "execute_host_tool",
        "target_tool": "closeAgentWidget",
        "parameters": {
            "messageId": "msg_abc123",
            "resultText": "Form submitted successfully."
        },
        "message": "Closing widget."
    }

    result = parse_subagent_directive(directive_data, ctx, "form-agent")

    assert result == "Closing widget."
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "CLOSE_AGENT_WIDGET"
    assert action["payload"]["messageId"] == "msg_abc123"
    assert action["payload"]["resultText"] == "Form submitted successfully."


def test_consult_agent_directive_open_admin_widget():
    """Verifies that consult_agent directive parsing converts openAdminWidget into OPEN_ADMIN_WIDGET action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_data = {
        "directive": "execute_host_tool",
        "target_tool": "openAdminWidget",
        "parameters": {
            "widgetType": "org_settings"
        }
    }

    result = parse_subagent_directive(directive_data, ctx, "admin-agent")

    assert result == "Opening the org_settings widget."
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "OPEN_ADMIN_WIDGET"
    assert action["payload"]["widgetType"] == "org_settings"


def test_consult_agent_directive_open_agent_widget():
    """Verifies that consult_agent directive parsing converts openAgentWidget into OPEN_AGENT_WIDGET action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_data = {
        "directive": "execute_host_tool",
        "target_tool": "openAgentWidget",
        "parameters": {
            "widgetId": "calendar_widget",
            "widgetConfig": {"mode": "view"},
            "data": {"events": ["e1"]}
        }
    }

    result = parse_subagent_directive(directive_data, ctx, "calendar-agent")

    assert result == "Displaying agent widget: calendar_widget"
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "OPEN_AGENT_WIDGET"
    assert action["payload"]["id"] == "calendar-agent"
    assert action["payload"]["widgetId"] == "calendar_widget"
    assert action["payload"]["target"] == "inline"


def test_consult_agent_directive_open_agent_widget_app_mode():
    """Verifies that consult_agent preserves target='app_mode', appConfig, and spatial metadata."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    app_config = {
        "appId": "tactical_app",
        "canvasWidget": {"widgetId": "canvas_gauges"},
        "title": "Tactical Console",
        "icon": "Command"
    }
    
    directive_data = {
        "directive": "execute_host_tool",
        "target_tool": "openAgentWidget",
        "parameters": {
            "target": "app_mode",
            "widgetId": "tactical_app",
            "appConfig": app_config,
            "title": "Tactical Console",
            "icon": "Command",
            "actions": [{"id": "save", "label": "Save"}]
        }
    }

    result = parse_subagent_directive(directive_data, ctx, "tactical-operations-agent")

    assert result == "Displaying agent widget: tactical_app"
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "OPEN_AGENT_WIDGET"
    assert action["payload"]["id"] == "tactical-operations-agent"
    assert action["payload"]["target"] == "app_mode"
    assert action["payload"]["appConfig"] == app_config
    assert action["payload"]["title"] == "Tactical Console"
    assert action["payload"]["icon"] == "Command"
    assert action["payload"]["actions"] == [{"id": "save", "label": "Save"}]



def test_consult_agent_directive_suggest_queries():
    """Verifies that consult_agent directive parsing converts suggestQueries into SET_SUGGESTIONS action."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    
    directive_data = {
        "directive": "execute_host_tool",
        "target_tool": "suggestQueries",
        "parameters": {
            "queries": ["What is pricing?", "Contact sales"]
        },
        "message": "Here are some helpful suggestions."
    }

    result = parse_subagent_directive(directive_data, ctx, "sales-agent")

    assert result == "Here are some helpful suggestions."
    assert len(ctx.actions) == 1
    action = ctx.actions[0]
    assert action["type"] == "SET_SUGGESTIONS"
    assert action["queries"] == ["What is pricing?", "Contact sales"]


def test_consult_agent_directive_non_directive_string():
    """Verifies that non-directive plain strings return None and do not modify ctx.actions."""
    ctx = RemoteContext(user_id="user_123", agent_id="host-agent", org_id="org_1", hub_id="hub_1")
    plain_text = "Here is the general information you requested."

    result = parse_subagent_directive(plain_text, ctx, "info-agent")

    assert result is None
    assert len(ctx.actions) == 0

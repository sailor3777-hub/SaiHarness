"""McpManager for tool tests: the result as the model sees it.

The server reports an error with the is_error flag and no prefix, and ToolRouter adds ERROR_PREFIX. Server tests check
error texts, so the prefix is added here the same way; the flag is returned as is.
"""
from core.markers import error
from core.mcp_manager import McpManager as _McpManager


class McpManager(_McpManager):
    def call(self, *args, **kwargs):
        text, is_error = super().call(*args, **kwargs)
        return (error(text) if is_error else text), is_error

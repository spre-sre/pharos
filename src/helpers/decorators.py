"""Tool-execution decorators shared by the MCP server."""

import logging

logger = logging.getLogger("lumino-mcp")


# Create a decorator to add tool execution logging
def log_tool_execution(func):
    """Decorator to log tool execution with tool name."""
    import functools

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        tool_name = func.__name__
        logger.info(f"Executing tool: {tool_name}")
        try:
            result = await func(*args, **kwargs)
            logger.info(f"Tool completed: {tool_name}")
            return result
        except Exception as e:
            logger.error(f"Tool failed: {tool_name} - Error: {str(e)}")
            raise

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        tool_name = func.__name__
        logger.info(f"Executing tool: {tool_name}")
        try:
            result = func(*args, **kwargs)
            logger.info(f"Tool completed: {tool_name}")
            return result
        except Exception as e:
            logger.error(f"Tool failed: {tool_name} - Error: {str(e)}")
            raise

    # Return appropriate wrapper based on whether function is async
    import asyncio
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper

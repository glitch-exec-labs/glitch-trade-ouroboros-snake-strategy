"""
Runtime compatibility shim for /opt/glitchexecutor/executor/ctrader_client.py.

That file imports ProtoOAOrderType and ProtoOATradeSide from
ctrader_open_api.messages.OpenApiMessages_pb2, but those two enums actually
live in OpenApiModelMessages_pb2 in every version of ctrader-open-api we've
tested (0.9.0 through 0.9.2). The result is that the production CTraderClient
silently sets PROTO_AVAILABLE=False and raises RuntimeError on instantiation.

Rather than edit the production file (we promised not to touch executor/),
we copy the missing symbols into OpenApiMessages_pb2 BEFORE anyone imports
executor.ctrader_client. Importing this module is a no-op once the patch
has been applied.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("ml_collector.ctrader_compat")


def apply() -> None:
    """Copy ProtoOAOrderType + ProtoOATradeSide into OpenApiMessages_pb2."""
    try:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as messages_mod
        import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_mod
    except ImportError as e:
        raise RuntimeError(
            "ctrader-open-api is not installed in this venv; "
            "run `pip install ctrader-open-api==0.9.0` and retry."
        ) from e

    patched = []
    for name in ("ProtoOAOrderType", "ProtoOATradeSide"):
        if not hasattr(messages_mod, name) and hasattr(model_mod, name):
            setattr(messages_mod, name, getattr(model_mod, name))
            patched.append(name)

    if patched:
        logger.info(
            "ctrader_compat: patched %s into OpenApiMessages_pb2",
            ", ".join(patched),
        )


# Apply immediately on import — any module that later imports
# executor.ctrader_client will see the patched symbols.
apply()

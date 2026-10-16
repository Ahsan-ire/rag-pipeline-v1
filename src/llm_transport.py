"""Transport reliability literals for the Anthropic clients (16A-2b b1a r5, D74).

The request timeout and the SDK retry count of each client change whether a call *succeeds*, never what a successful
call returns. They are the only values here and this module holds nothing behavioural: no model id, no prompt, no
token cap, no parsing. It is registered in ``src.pipeline_config.CODE_EXCLUDED``, so editing a literal below does not
change ``pipeline_config_hash`` (the incumbent's identity) or ``judge_identity`` -- the exclusion the pre-registration
records as ``timeouts_retries``.

``src.generator.generation_llm_kwargs`` (generation and the judge, which calls through the same client) and
``src.query_rewrite.rewrite_llm_kwargs`` (the Haiku rewrite client) import these.
"""

#: Generation (and judge) client: seconds before a blocked read times out. A dead socket must fail loudly, not hang
#: the caller forever: two canonical-eval runs on 17 Jul wedged for hours on a single blocked read because no client
#: timeout was set (D52). 120 s covers the slowest observed generation call with ample headroom.
GENERATION_REQUEST_TIMEOUT = 120.0
#: Generation (and judge) client: the SDK transparently retries timeouts and connection drops this many times
#: before raising, so a transient drop self-heals (D52).
GENERATION_MAX_RETRIES = 3

#: Rewrite client: expansion calls run about 2 s, so 60 s is generous; a blocked read times out and retries instead of
#: wedging the caller (D52).
REWRITE_REQUEST_TIMEOUT = 60.0
#: Rewrite client: the same retry contract as generation.
REWRITE_MAX_RETRIES = 3

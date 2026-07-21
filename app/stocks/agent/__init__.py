"""The research-agent slice: a Claude-driven tool-use loop over the stocks feature.

Unlike the analysis slices — each a single forced-tool call that turns already-gathered
figures into one structured read — this slice lets the model *drive*: given a plain-English
question it decides which tools to call, reads their results, and calls more until it can
answer. The tools are the app's own read use cases (screen the universe, read market
sentiment), so the agent can only ever surface real, screened data — never a stock it made up.

Layered like every other slice (dependencies point inward):

* ``entities`` — the conversation primitives (a model turn, a tool call, a tool result) and
  the finished ``ResearchResult``. Pure data, no vendor, no framework.
* ``ports`` — ``ConversationModel`` (one model turn) and ``Tool`` (one callable capability).
* ``use_cases`` — ``RunResearch`` owns the loop: call the model, run any tools it asked for,
  feed the results back, repeat to a bounded step limit, then return the answer.
* ``tools`` — the concrete capabilities, each delegating to another slice's read use case.
* ``adapters/bedrock/research_model_adapter`` — the only code that knows Bedrock: it turns our
  conversation entities into an Anthropic multi-turn tool call and back.

The loop lives in the use case (application policy: the step budget, tool dispatch, error
recovery), so it's fully testable offline against a scripted fake model — the vendor adapter
stays a stateless per-turn translator.
"""

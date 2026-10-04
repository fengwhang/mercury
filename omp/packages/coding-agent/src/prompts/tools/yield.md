Submit subagent output. Always wrap the payload: `result: { data: <your output> }` for success, `result: { error: "message" }` only when the task actually failed. Putting a completed report in `error` marks the task aborted and tells the parent it failed. Never serialize a success envelope into `error`.

Omit `type` for the usual single terminal structured result. Pass `type: ["section"]` to submit an incremental, non-terminal section that accumulates.
{{#if hasOutputSchema}}
This task declares an output schema: the terminal `result.data` MUST be the full object matching it. A data-less `type: "result"` finalizes previously submitted incremental sections; it is invalid when no sections were submitted — prose in your last turn can never satisfy the schema.
{{else}}
`result.data` accepts plain-text reports, objects, arrays, numbers, or booleans. For a prose report, use `{"result":{"data":"Work completed. Here are the findings…"}}`; you do not need to JSON-encode an object inside a string.
Pass `type: "result"` to finalize; when `data` is omitted, your last assistant turn becomes the raw final result.
{{/if}}

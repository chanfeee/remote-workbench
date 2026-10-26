// Generated from edit-request.json; replace edit_id with read_file(for_edit=true).
{const r=await tools.mcp__remote_workbench__apply_patch(JSON.parse("{\"cwd\":\"/remote/project\",\"edits\":[{\"edit_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"start_line\":1,\"end_line\":1,\"new\":[\"print(\\\"new\\\")\"]}]}"));
if(r.isError) text("Remote Workbench error");
if(Array.isArray(r.content)){for(const c of r.content) if(c.type==="text") text(c.text);}else text(r);}

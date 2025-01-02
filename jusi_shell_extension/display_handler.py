import subprocess, os, tempfile

class ShellHandler:
    @staticmethod
    def handle(content, bufnr, sign_id, *args, **kwargs):
        os.environ['HISTCONTROL'] = 'ignorespace'
        bash_commands = r'''
jsonify() {{
    awk '
    function json_escape(s, r, i, c) {{
        r = ""
        for (i = 1; i <= length(s); i++) {{
            c = substr(s, i, 1)
            if (c == "\\") {{
                r = r "\\\\"
            }} else if (c == "\"") {{
                r = r "\\\""
            }} else if (c == "\b") {{
                r = r "\\b"
            }} else if (c == "\f") {{
                r = r "\\f"
            }} else if (c == "\n") {{
                r = r "\\n"
            }} else if (c == "\r") {{
                r = r "\\r"
            }} else if (c == "\t") {{
                r = r "\\t"
            }} else {{
                r = r c
            }}
        }}
        return r
    }}
    {{
        escaped = json_escape($0)
        printf "%s\"%s\"", (NR==1?"":","), escaped
    }}
    '
}}

vimcompgen() {{
    completions=$({{ compgen -d -S '/' -- $1; compgen -f -- $1 | while read -r item; do [ -f "$item" ] && echo "$item"; done; }} | sort | jsonify)
    #completions=$(compgen -f -- $1 | python -c 'import json,sys;print(json.dumps(sys.stdin.read().splitlines()))')
    printf "\e]51;[\\\"call\\\", \\\"Jusiapi_ShellComplete\\\", [$completions]]\\x07"
}}

{content}

        '''
        bash_cmds = bash_commands.format(content=content)
        #with tempfile.NamedTemporaryFile(mode='w', delete=False) as rcfile:
        with open('testrcfunc', 'w') as rcfile:
            rcfile_name = rcfile.name
            rcfile.write(bash_cmds)
        os.system(r'printf "\e]51;[\"call\", \"Jusiapi_SetStatus\", $(echo [\"' + str(bufnr) + r'\", \"' + str(sign_id) + r'\", \"cellfollowup\"])]\x07"')
        subprocess.call(['bash', '--rcfile', rcfile_name])
        #os.unlink(rcfile_name)


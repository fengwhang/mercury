"""Saved Setup configuration reaches calls without any browser credentials."""
import json
import os
from pathlib import Path
import pytest

from mercury_cli import config, setup
from observatory import mlounge, voice_call_stt


def test_saved_setup_bootstraps_private_authenticated_service(tmp_path, monkeypatch):
    install = tmp_path / '.mercury-nightly'
    profile = install / 'hermes/profiles/voice'
    profile.mkdir(parents=True)
    monkeypatch.setenv('MERCURY_HOME', str(install))
    monkeypatch.setenv('HERMES_HOME', str(profile))
    monkeypatch.setenv('MERCURY_CONFIG', str(profile / 'config.yaml'))
    monkeypatch.setenv('HERMES_PROFILE', 'voice')
    monkeypatch.setenv('MERCURY_INHERIT_FROM', str(tmp_path / 'absent'))
    cfg = {'tts': {'provider': 'fixture-tts', 'providers': {'fixture-tts': {'type': 'command', 'command': 'fixture'}}}}
    setup.apply_voice_call_stt_selection(cfg, provider='parakeet', model='existing-assets', language='fr', command='explicit-fixture {input_path}')
    setup.apply_voice_call_hosts(cfg, mirc_host_url='https://mirc.example', mlounge_host_url='https://lounge.example', stt_sidecar_url='http://lounge.example:8765')
    config.save_config(cfg)
    config.save_env_value('VOICE_CALL_MIRC_TOKEN', 'private-split-host-fixture-secret')
    bootstrap = getattr(mlounge, 'configure_voice_call_service', None)
    assert callable(bootstrap), 'Setup/provisioner has no authenticated call service bootstrap'
    result = bootstrap(install, username='owner', network={'uuid': 'owned-network', 'host': 'irc.example', 'port': 6697, 'tls': True}, hermes_root=str(Path(__file__).resolve().parents[2]))
    private = json.loads(Path(result['config']).read_text())
    assert private['profile'] == 'voice'
    assert private['configPath'] == str(profile / 'config.yaml')
    assert private['origin'] == 'https://lounge.example'
    assert private['users'] == ['owner']
    assert private['network'] == {'uuid': 'owned-network', 'host': 'irc.example', 'port': 6697, 'tls': True}
    secrets = config.load_env()
    assert secrets['VOICE_CALL_SIDECAR_TOKEN'] != secrets['VOICE_CALL_MIRC_TOKEN']
    assert 'sidecarToken' not in private and 'mircToken' not in private
    assert private['envPath'] == str(config.get_env_path())
    assert os.stat(result['config']).st_mode & 0o777 == 0o600
    assert 'existing-assets' not in json.dumps(result)
    assert secrets['VOICE_CALL_SIDECAR_TOKEN'] not in json.dumps(result)
    assert config.load_config()['stt']['parakeet']['language'] == 'fr'
    unit = Path(result['unit']).read_text()
    assert str(profile / 'config.yaml') in unit
    assert 'observatory.voice_call_stt' in unit
    assert secrets['VOICE_CALL_SIDECAR_TOKEN'] not in unit
    assert 'EnvironmentFile=' in unit
    original = secrets['VOICE_CALL_SIDECAR_TOKEN']
    bootstrap(install, username='owner', network=private['network'], hermes_root=str(Path(__file__).resolve().parents[2]))
    assert config.load_env()['VOICE_CALL_SIDECAR_TOKEN'] == original


def test_disabled_stt_does_not_dispatch(monkeypatch):
    from tools import transcription_tools
    monkeypatch.setattr(transcription_tools, '_dispatch_stt_provider', lambda *_: (_ for _ in ()).throw(AssertionError('provider called')))
    result = voice_call_stt.transcribe_chunk(b'RIFF-fixture', 'audio/wav', {'enabled': False, 'provider': 'parakeet'})
    assert not result['success']
    assert 'disabled' in result['error'].lower()


def test_sidecar_auth_is_service_header_only_and_fail_closed():
    from types import SimpleNamespace
    state = voice_call_stt.SidecarState('http://mirc.example', {}, token='private-fixture')
    assert not state.check_token(SimpleNamespace(path='/call?token=private-fixture', headers={}))
    assert not state.check_token(SimpleNamespace(path='/call', headers={'Origin': 'https://evil', 'X-Voice-Call-Token': 'private-fixture'}))
    assert state.check_token(SimpleNamespace(path='/call', headers={'X-Voice-Call-Token': 'private-fixture'}))
    empty = voice_call_stt.SidecarState('http://mirc.example', {})
    assert not empty.check_token(SimpleNamespace(path='/call', headers={}))


def test_mirc_service_cannot_select_foreign_profile(monkeypatch):
    from fastapi.testclient import TestClient
    from mercury_cli import web_server
    monkeypatch.setenv('VOICE_CALL_MIRC_TOKEN', 'private-service')
    monkeypatch.setenv('HERMES_PROFILE', 'voice')
    monkeypatch.setattr(web_server.app.state, 'auth_required', True, raising=False)
    client = TestClient(web_server.app)
    response = client.post('/api/audio/speak?profile=foreign', headers={'Authorization': 'Bearer private-service'}, json={'text': 'never synthesize'})
    assert response.status_code == 403


@pytest.mark.parametrize("selected_profile", ["default", "voice"])
def test_saved_setup_real_canonical_audio_over_authenticated_socket(tmp_path, monkeypatch, selected_profile):
    import base64
    import io
    import shlex
    import sys
    import threading
    import wave
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from fastapi.testclient import TestClient
    from websockets.sync.client import connect
    from mercury_cli import web_server
    from observatory import voice_call
    from tools import transcription_tools, tts_tool

    install = tmp_path / '.mercury-nightly'
    runtime = install if selected_profile == 'default' else install / 'hermes/profiles' / selected_profile
    runtime.mkdir(parents=True)
    fixture_model = 'existing-assets-' + selected_profile
    tts_provider = 'fixture-tts-' + selected_profile
    for key, value in {'MERCURY_HOME': str(install), 'HERMES_HOME': str(runtime),
                       'MERCURY_CONFIG': str(runtime / 'config.yaml'),
                       'HERMES_PROFILE': selected_profile}.items():
        monkeypatch.setenv(key, value)
    fixture = tmp_path / 'audio_provider.py'
    fixture.write_text(
        "import sys,pathlib,wave\n"
        "kind,source,output,model,language=sys.argv[1:]\n"
        f"assert model=={fixture_model!r} and language=='fr'\n"
        "if kind=='stt':\n"
        " with wave.open(source) as w: assert w.getnframes()==1600\n"
        " pathlib.Path(output,'transcript.txt').write_text('bonjour from browser')\n"
        "else:\n"
        " assert pathlib.Path(source).read_text()=='Hermes reply to bonjour from browser'\n"
        " with wave.open(output,'wb') as w:\n"
        "  w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\\x00\\x01'*1600)\n"
    )
    prefix = shlex.quote(sys.executable) + ' ' + shlex.quote(str(fixture))
    cfg = {'tts': {'provider': tts_provider, 'providers': {tts_provider: {
        'type': 'command', 'command': prefix + ' tts {input_path} {output_path} {model} {voice}',
        'model': fixture_model, 'voice': 'fr', 'output_format': 'wav'}}}}
    setup.apply_voice_call_stt_selection(cfg, provider='parakeet', model=fixture_model, language='fr',
        command=prefix + ' stt {input_path} {output_dir} {model} {language}')
    cfg['stt']['enabled'] = True
    config.save_config(cfg)
    config.save_env_value('VOICE_CALL_MIRC_TOKEN', 'mirc-fixture-private')
    config.save_env_value('VOICE_CALL_SIDECAR_TOKEN', 'sidecar-fixture-private')
    store = voice_call.VoiceCallStore()
    monkeypatch.setattr(voice_call, 'default_store', lambda: store)
    monkeypatch.setattr(voice_call, 'resolve_channel_agent', lambda _: {
        'engine': 'hermes', 'name': 'Thermometer', 'room_id': '#voice', 'profile': selected_profile})
    monkeypatch.setattr(web_server.app.state, 'auth_required', True, raising=False)
    make_app = getattr(voice_call, 'create_voice_service_app', None)
    assert callable(make_app), 'Setup has no managed authenticated MIRC voice API'
    dashboard = TestClient(make_app())
    effects = []
    original_dispatch = transcription_tools._dispatch_stt_provider
    def dispatch(*args, **kwargs):
        assert args[2]['providers']['parakeet']['model'] == fixture_model
        effects.append(('stt', args[1], args[4]))
        return original_dispatch(*args, **kwargs)
    monkeypatch.setattr(transcription_tools, '_dispatch_stt_provider', dispatch)
    assert tts_tool._get_provider(tts_tool._load_tts_config()) == tts_provider

    class Mirc(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def serve(self, method):
            headers = {'Authorization': self.headers.get('Authorization', '')}
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0))) if method == 'POST' else None
            response = dashboard.request(method, self.path, headers=headers, json=body)
            raw = response.content
            self.send_response(response.status_code)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            self.serve('GET')
        def do_POST(self):
            self.serve('POST')

    mirc = ThreadingHTTPServer(('127.0.0.1', 0), Mirc)
    mirc.daemon_threads = True
    state = voice_call_stt.SidecarState(
        f'http://127.0.0.1:{mirc.server_port}', voice_call_stt.load_sidecar_stt_config(),
        token=config.get_env_value('VOICE_CALL_SIDECAR_TOKEN'),
        mirc_token=config.get_env_value('VOICE_CALL_MIRC_TOKEN'), profile=selected_profile, reload_config=True)
    class Handler(voice_call_stt.SidecarHandler):
        pass
    Handler.state = state
    sidecar = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    sidecar.daemon_threads = True
    threads = [threading.Thread(target=server.serve_forever) for server in (mirc, sidecar)]
    for thread in threads:
        thread.start()
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x01' * 1600)
    try:
        url = f'ws://127.0.0.1:{sidecar.server_port}/call'
        with connect(url, additional_headers={'X-Voice-Call-Token': 'sidecar-fixture-private'}) as ws:
            ws.send(json.dumps({'type': 'hello', 'channel': '#voice', 'mime': 'audio/wav'}))
            ready = json.loads(ws.recv(timeout=5))
            assert ready['type'] == 'ready' and ready['sttProvider'] == 'parakeet'
            ws.send(audio.getvalue())
            transcript = json.loads(ws.recv(timeout=5))
            assert transcript['text'] == 'bonjour from browser'
            # The unchanged Hermes turn is an injected offline boundary; no paid LLM.
            from run_agent import AIAgent
            monkeypatch.setattr(AIAgent, 'run_conversation',
                lambda self, user_message, **_: {'final_response': 'Hermes reply to ' + user_message})
            reply = AIAgent.run_conversation(object(), user_message=transcript['text'])['final_response']
            ws.send(json.dumps({'type': 'tts', 'callId': ready['callId'], 'text': reply, 'token': 'reply-1'}))
            speech = json.loads(ws.recv(timeout=5))
            assert speech['type'] == 'audio', speech
            assert speech['provider'] == tts_provider
            decoded = base64.b64decode(speech['dataUrl'].split(',', 1)[1])
            with wave.open(io.BytesIO(decoded)) as wav:
                assert wav.getnframes() == 1600 and wav.getframerate() == 16000
            assert effects == [('stt', 'parakeet', 'voice-call')]
            ws.send(json.dumps({'type': 'hangup', 'callId': ready['callId']}))
            assert json.loads(ws.recv(timeout=5))['type'] == 'ended'
        assert not state.calls
        import subprocess
        lounge = Path(__file__).resolve().parents[3] / 'third_party' / 'mlounge'
        if (lounge / 'dist/server/voice-call.js').is_file():
            setup.apply_voice_call_hosts(cfg,
                mirc_host_url=f'http://127.0.0.1:{mirc.server_port}',
                mlounge_host_url='https://lounge.example',
                stt_sidecar_url=f'http://127.0.0.1:{sidecar.server_port}')
            config.save_config(cfg)
            bootstrap = mlounge.configure_voice_call_service(
                install, username='owner', network={'uuid': 'owned-network', 'host': 'irc.example', 'port': 6697, 'tls': True},
                hermes_root=str(Path(__file__).resolve().parents[2]))
            audio_path = tmp_path / 'browser.wav'
            audio_path.write_bytes(audio.getvalue())
            child_env = dict(os.environ, MERCURY_VOICE_CALL_CONFIG=bootstrap['config'],
                FIXTURE_AUDIO=str(audio_path), FIXTURE_MLOUNGE_HOME=str(tmp_path / 'private-lounge'),
                FIXTURE_TTS_PROVIDER=tts_provider)
            process = subprocess.run(['node', str(lounge / 'test/fixtures/voice-call-roundtrip.cjs')],
                env=child_env, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, close_fds=True)
            assert process.returncode == 0, process.stderr
            proof_line = next(line for line in process.stdout.splitlines() if line.startswith('VOICE_CALL_PROOF='))
            proof = json.loads(proof_line.partition('=')[2])
            assert proof['ready'] and proof['ended'] and proof['audioBytes'] == 3244
            assert effects == [('stt', 'parakeet', 'voice-call')] * 2
            print('Authenticated compiled relay / canonical tools:', proof_line)
        for stream in ('audio', 'tts'):
            monkeypatch.setattr(voice_call, 'resolve_channel_agent', lambda _: {
                'engine': 'hermes', 'name': 'Thermometer', 'room_id': '#voice', 'profile': selected_profile})
            before = list(effects)
            with connect(url, additional_headers={'X-Voice-Call-Token': 'sidecar-fixture-private'}) as ws:
                ws.send(json.dumps({'type': 'hello', 'channel': '#voice', 'mime': 'audio/wav'}))
                active = json.loads(ws.recv(timeout=5))
                assert active['type'] == 'ready'
                monkeypatch.setattr(voice_call, 'resolve_channel_agent', lambda _: {
                    'engine': 'omp', 'name': 'Coder', 'room_id': '#voice', 'profile': selected_profile})
                ws.send(audio.getvalue() if stream == 'audio' else json.dumps({
                    'type': 'tts', 'callId': active['callId'],
                    'text': 'Hermes reply to bonjour from browser', 'token': 'reply-2'}))
                assert json.loads(ws.recv(timeout=5))['type'] == 'refused'
            assert effects == before
        monkeypatch.setattr(voice_call, 'resolve_channel_agent', lambda _: {
            'engine': 'omp', 'name': 'Coder', 'room_id': '#voice', 'profile': selected_profile})
        before = list(effects)
        with connect(url, additional_headers={'X-Voice-Call-Token': 'sidecar-fixture-private'}) as ws:
            ws.send(json.dumps({'type': 'hello', 'channel': '#voice', 'mime': 'audio/wav'}))
            assert json.loads(ws.recv(timeout=5))['type'] == 'refused'
        assert effects == before
        assert dashboard.post('/api/audio/speak?profile=foreign',
            headers={'Authorization': 'Bearer mirc-fixture-private'}, json={'text': 'never'}).status_code == 403
        assert dashboard.post('/api/audio/speak', json={'text': 'never'}).status_code == 401
        assert dashboard.post('/api/audio/speak',
            headers={'Authorization': 'Bearer mirc-fixture-private', 'Origin': 'https://evil'},
            json={'text': 'never'}).status_code == 401
        assert dashboard.get('/api/config?profile=' + selected_profile,
            headers={'Authorization': 'Bearer mirc-fixture-private'}).status_code == 404
    finally:
        for server in (sidecar, mirc):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
        dashboard.close()


def test_setup_rerun_keeps_canonical_command_and_refreshes_profile_service(tmp_path, monkeypatch):
    from types import SimpleNamespace
    install = tmp_path / '.mercury-nightly'
    profile = install / 'hermes/profiles/voice'
    profile.mkdir(parents=True)
    for key, value in {'MERCURY_HOME': str(install), 'HERMES_HOME': str(profile),
                       'MERCURY_CONFIG': str(profile / 'config.yaml'), 'HERMES_PROFILE': 'voice'}.items():
        monkeypatch.setenv(key, value)
    cfg = {}
    setup.apply_voice_call_stt_selection(cfg, provider='parakeet', command='saved-custom-cli {input_path} {model} {language}')
    setup.apply_voice_call_hosts(cfg, mirc_host_url='http://lounge.example:8123',
        mlounge_host_url='https://lounge.example', stt_sidecar_url='http://lounge.example:8765')
    config.save_config(cfg)
    first = mlounge.configure_voice_call_service(install, username='owner',
        network={'uuid': 'owned-network', 'host': 'irc.example', 'port': 6697, 'tls': True}, hermes_root=str(Path(__file__).resolve().parents[2]))
    monkeypatch.setattr(mlounge, '_systemctl_available', lambda: False)
    monkeypatch.setattr(mlounge, 'mlounge_unit_active', lambda: False)
    monkeypatch.setattr(mlounge, 'mlounge_bin', lambda *_: Path('/existing/mlounge'))
    setup.setup_stt(config.load_config(), SimpleNamespace(non_interactive=True,
        stt_provider='parakeet', stt_model='existing-replacement-assets', stt_language='de',
        stt_endpoint='', mirc_url='', mlounge_url='', sidecar_url=''))
    loaded = config.load_config()
    assert loaded['stt']['providers']['parakeet']['command'] == 'saved-custom-cli {input_path} {model} {language}'
    assert loaded['stt']['providers']['parakeet']['model'] == 'existing-replacement-assets'
    assert loaded['stt']['providers']['parakeet']['language'] == 'de'
    private = json.loads(Path(first['config']).read_text())
    assert private['configPath'] == str(profile / 'config.yaml')
    assert 'VOICE_CALL_' not in json.dumps(private)
    assert Path(first['mirc_unit']).is_file()
    for unit in ('mercury-nightly-voice-call.service', 'mercury-nightly-mirc-voice.service'):
        generated = (Path.home() / '.config/systemd/user' / unit).read_text()
        assert str(profile / 'config.yaml') in generated
        assert 'TOKEN=' not in generated


def test_disabled_voice_service_refuses_before_handlers(monkeypatch):
    from fastapi.testclient import TestClient
    from observatory import voice_call
    monkeypatch.setenv('VOICE_CALL_MIRC_TOKEN', 'private-disabled-fixture')
    monkeypatch.setattr(voice_call, 'voice_call_config', lambda *_: {'enabled': False})
    with TestClient(voice_call.create_voice_service_app()) as client:
        response = client.get('/api/voice-call/status',
            headers={'Authorization': 'Bearer private-disabled-fixture'})
    assert response.status_code == 503


def test_setup_does_not_replace_saved_custom_provider(monkeypatch):
    from types import SimpleNamespace
    cfg = {'stt': {'provider': 'custom-whisper', 'enabled': False,
                   'providers': {'custom-whisper': {'type': 'command', 'local': True, 'command': 'saved-whisper {input_path}'}}}}
    monkeypatch.setattr(setup, '_refresh_configured_voice_call', lambda: None)
    setup.setup_stt(cfg, SimpleNamespace(non_interactive=True,
        stt_provider='', stt_model='', stt_language='', stt_endpoint='',
        mirc_url='', mlounge_url='', sidecar_url=''))
    assert cfg['stt']['provider'] == 'custom-whisper'
    assert cfg['stt']['enabled'] is False
    assert cfg['stt']['providers']['custom-whisper']['command'] == 'saved-whisper {input_path}'


def test_setup_missing_provider_never_selects_cloud(monkeypatch):
    from types import SimpleNamespace
    cfg = {}
    monkeypatch.setattr(setup, '_refresh_configured_voice_call', lambda: None)
    setup.setup_stt(cfg, SimpleNamespace(non_interactive=True,
        stt_provider='', stt_model='', stt_language='', stt_endpoint='',
        mirc_url='', mlounge_url='', sidecar_url=''))
    assert not (cfg.get('stt') or {}).get('provider')


def test_interactive_setup_saves_explicit_host_configuration(monkeypatch):
    cfg = {'stt': {'provider': 'custom-whisper'}, 'voice_call': {'enabled': False}}
    answers = iter(('https://mirc.example', 'https://lounge.example', 'https://stt.example'))
    monkeypatch.setattr(setup, 'is_interactive_stdin', lambda: True)
    monkeypatch.setattr(setup, 'prompt', lambda *_args, **_kwargs: next(answers))
    setup._setup_voice_call_hosts(cfg)
    assert cfg['voice_call']['mirc_host_url'] == 'https://mirc.example'
    assert cfg['voice_call']['mlounge_host_url'] == 'https://lounge.example'
    assert cfg['voice_call']['stt_sidecar_url'] == 'https://stt.example'
    assert cfg['voice_call']['enabled'] is False
    assert cfg['stt']['provider'] == 'custom-whisper'

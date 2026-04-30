# DiscoBot

## Riproduzione MIDI
Per la riproduzione dei midi è necessario inserire un SoundFont all'interno della cartella soundfonts/ 

Si suggerisce come pack equilibrato GeneralUser GS

## Riproduzione diretta da Spotify
Per riprodurre tracce direttamente dallo Spotify ufficiale (non più via proxy YouTube)
serve un account **Spotify Premium**. La prima volta esegui il login OAuth:

```
python main.py --spotify-login
```

Si apre il browser sulla pagina di autorizzazione Spotify; al consenso le credenziali
vengono salvate in `.spotify_credentials.json` (gitignored). Dai riavvii successivi
DiscoBot riusa il file in automatico — niente più login finché il token resta valido.
Se l'autenticazione decade, l'endpoint `/spotify/stream/...` risponde 401, il player
salta la traccia e basta rilanciare `--spotify-login`.

### Login remoto via Spotify Connect
In serata, se sei sul dancefloor col telefono e il token decade, non serve tornare
al kiosk. Apri la webapp di DiscoBot, clicca l'ingranaggio ⚙ in alto, sezione **Spotify**,
poi **Login Spotify Connect**:

1. DiscoBot espone un dispositivo "DiscoBot" via mDNS sulla LAN
2. Apri Spotify sul telefono → Dispositivi → seleziona **DiscoBot**
3. Spotify trasmette le credenziali a librespot, che le persiste nello stesso file

L'indicatore Spotify nell'header torna verde entro un paio di secondi e la riproduzione
riprende. Il dispositivo si auto-spegne dopo il primo capture, così DiscoBot non resta
visibile come Connect device permanente. Telefono e kiosk devono essere sulla stessa LAN
(per il broadcast mDNS).


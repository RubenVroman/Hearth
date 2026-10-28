# webOS package

`com.hearth.tv/` is the IPK root for the living-room LG (OLED65G1RLA, webOS 6).

The package does not contain the command center. It stores the Hearth origin locally and opens `http://<vault>:8787/tv` fullscreen. Sign-in and the machine token live on that page.

Pack and install steps: [docs/webos.md](../docs/webos.md).

```bash
npm install -g @webos-tools/cli
ares-package webos/com.hearth.tv -o dist
ares-install --device lgwebostv dist/com.hearth.tv_1.0.0_all.ipk
ares-launch --device lgwebostv com.hearth.tv
```

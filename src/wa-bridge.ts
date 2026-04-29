/// <reference types="@workadventure/iframe-api-typings" />

/**
 * Live Armin · WorkAdventure scripting entry (loader stub).
 *
 * The map's Tiled `script` property points at this file so that the
 * wa-map-optimizer-vite build can bundle a wrapper HTML page (the optimizer
 * cannot resolve a remote URL as an entry).
 *
 * The real bridge logic lives in the LiveArmin Next.js app at
 *   public/scripting/wa-bridge.js
 * and is hosted at https://live-armin.vercel.app/scripting/wa-bridge.js so it
 * can be updated without re-uploading the map. We just inject a <script> tag
 * here at runtime to load it inside the WA player iframe.
 *
 * Override the source for local QA via a hash parameter on the room URL:
 *   ?bridge=https://my-tunnel.example/scripting/wa-bridge.js
 */

const DEFAULT_BRIDGE_URL =
    "https://live-armin.vercel.app/scripting/wa-bridge.js";

function resolveBridgeUrl(): string {
    try {
        const params = (WA?.room?.hashParameters ?? {}) as Record<string, string>;
        if (params.bridge) return String(params.bridge);
    } catch {
        // hashParameters may not be available before WA.onInit; fall through.
    }
    return DEFAULT_BRIDGE_URL;
}

WA.onInit()
    .then(() => {
        const src = resolveBridgeUrl();
        console.info("[live-armin] loading bridge from", src);
        const tag = document.createElement("script");
        tag.src = src;
        tag.async = true;
        tag.onerror = (err) =>
            console.error("[live-armin] failed to load bridge", src, err);
        document.head.appendChild(tag);
    })
    .catch((err) => console.error("[live-armin] WA.onInit failed", err));

export {};

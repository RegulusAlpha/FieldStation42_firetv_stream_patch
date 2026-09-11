// Form-based editor for the common properties of a station config.
//
// Deliberately NOT a generic JSON-Schema-driven form generator: the field
// list below is hand-picked to cover the properties people actually edit
// often. Anything not listed here (day_templates/weekday schedules,
// slot_overrides, tag_overrides, date_overrides, autobump, clip_shows,
// streams) stays JSON-only (or, for day/hour scheduling, lives in the
// Schedule tab instead) - see docs/STATION_UI_PLAN.md's Phase 3 scoping.

(function () {
    const VIDEO_SCRAMBLE_OPTIONS = [
        "", "horizontal_line", "diagonal_lines", "static_overlay", "pixel_block",
        "color_inversion", "severe_noise", "wavy", "random_block", "chunky_scramble", "spicy"
    ];

    // Each field: { key, label, tab, type, options?, appliesTo?, help? }
    // appliesTo: list of network_type values this field is relevant for.
    // Omit appliesTo to show the field for every network type.
    const FIELDS = [
        // --- Basic Info ---
        { key: "network_name", label: "Network Name", tab: "basic", type: "text", required: true },
        { key: "network_long_name", label: "Long Name", tab: "basic", type: "text" },
        { key: "channel_number", label: "Channel Number", tab: "basic", type: "number", required: true },
        { key: "network_type", label: "Network Type", tab: "basic", type: "select",
          options: ["standard", "web", "guide", "loop", "streaming"] },
        { key: "hidden", label: "Hidden (skip when channel surfing)", tab: "basic", type: "checkbox" },
        { key: "schedule_increment", label: "Schedule Increment (minutes)", tab: "basic", type: "number",
          appliesTo: ["standard", "loop"] },
        { key: "schedule_offset", label: "Schedule Offset (minutes)", tab: "basic", type: "number",
          appliesTo: ["standard", "loop"] },

        // --- Content & Paths ---
        { key: "content_dir", label: "Content Directory", tab: "content", type: "text",
          appliesTo: ["standard", "loop"] },
        { key: "bump_dir", label: "Bump Directory", tab: "content", type: "text", appliesTo: ["standard"] },
        { key: "commercial_dir", label: "Commercial Directory", tab: "content", type: "text", appliesTo: ["standard"] },
        { key: "runtime_dir", label: "Runtime Directory", tab: "content", type: "text" },
        { key: "standby_image", label: "Standby Image", tab: "content", type: "text" },
        { key: "be_right_back_media", label: "Be-Right-Back Media", tab: "content", type: "text" },
        { key: "sign_off_video", label: "Sign-Off Video", tab: "content", type: "text" },
        { key: "off_air_video", label: "Off-Air Video", tab: "content", type: "text" },
        { key: "fallback_tag", label: "Fallback Tag", tab: "content", type: "text",
          appliesTo: ["standard"], help: "Folder/tag to use when no content matches a scheduled slot." },
        { key: "web_url", label: "Web Page URL", tab: "content", type: "text", appliesTo: ["web"] },
        { key: "refresh_interval", label: "Refresh Interval (seconds)", tab: "content", type: "number", appliesTo: ["web"] },
        { key: "shuffle_loop", label: "Shuffle Loop", tab: "content", type: "checkbox", appliesTo: ["loop"],
          help: "Play every video once before reshuffling, instead of alphabetical looping." },
        { key: "messages", label: "Messages (one per line)", tab: "content", type: "lines", appliesTo: ["guide"] },
        { key: "images", label: "Images (one path per line)", tab: "content", type: "lines", appliesTo: ["guide"] },
        { key: "streams", label: "Streams (advanced JSON)", tab: "content", type: "json", appliesTo: ["streaming"],
          help: "Array of {url, duration, title}." },

        // --- Playback ---
        { key: "commercial_free", label: "Commercial Free", tab: "playback", type: "checkbox", appliesTo: ["standard"] },
        { key: "break_strategy", label: "Break Strategy", tab: "playback", type: "select",
          options: ["standard", "end", "center"], appliesTo: ["standard"] },
        { key: "break_duration", label: "Break Duration (seconds)", tab: "playback", type: "number", appliesTo: ["standard"] },
        { key: "video_keepaspect", label: "Keep Aspect Ratio", tab: "playback", type: "checkbox" },
        { key: "panscan", label: "Panscan", tab: "playback", type: "number" },
        { key: "media_filter", label: "Media Filter", tab: "playback", type: "select",
          options: ["video", "audio", "mixed"] },
        { key: "video_scramble_fx", label: "Video Scramble Effect", tab: "playback", type: "select",
          options: VIDEO_SCRAMBLE_OPTIONS },
        { key: "station_fx", label: "Custom FFMPEG Filter", tab: "playback", type: "text",
          help: "Ignored if Video Scramble Effect is set." },
        { key: "fullscreen", label: "Fullscreen", tab: "playback", type: "checkbox", appliesTo: ["guide"] },
        { key: "width", label: "Window Width", tab: "playback", type: "number", appliesTo: ["guide"] },
        { key: "height", label: "Window Height", tab: "playback", type: "number", appliesTo: ["guide"] },
        { key: "scroll_speed", label: "Scroll Speed", tab: "playback", type: "number", appliesTo: ["guide"] },
        { key: "play_sound", label: "Play Sound", tab: "playback", type: "checkbox", appliesTo: ["guide"] },
        { key: "sound_to_play", label: "Sound To Play", tab: "playback", type: "text", appliesTo: ["guide"] },
        { key: "logo_dir", label: "Logo Directory", tab: "playback", type: "text" },
        { key: "show_logo", label: "Show Logo", tab: "playback", type: "checkbox" },
        { key: "default_logo", label: "Default Logo Filename", tab: "playback", type: "text" },
        { key: "logo_permanent", label: "Logo Always Visible", tab: "playback", type: "checkbox" },

        // --- Advanced ---
        { key: "clip_shows", label: "Clip Shows (advanced JSON)", tab: "advanced", type: "json", appliesTo: ["standard"] },
        { key: "autobump", label: "Autobump (advanced JSON)", tab: "advanced", type: "json" },
        { key: "active_rules", label: "Active Rules (advanced JSON)", tab: "advanced", type: "json",
          help: '{"date_range": "December 1 - January 2"} to only load this config in that window.' },
    ];

    const TABS = [
        { id: "basic", label: "Basic Info" },
        { id: "content", label: "Content & Paths" },
        { id: "playback", label: "Playback" },
        { id: "advanced", label: "Advanced" },
    ];

    function fieldApplies(field, networkType) {
        return !field.appliesTo || field.appliesTo.includes(networkType);
    }

    function linesToArray(text) {
        return text.split("\n").map(s => s.trim()).filter(s => s.length > 0);
    }

    function renderFieldInput(field, stationConf, onChange) {
        const value = stationConf[field.key];
        const id = `f42form_${field.key}`;
        const wrap = document.createElement("div");
        wrap.className = "pure-control-group";

        const label = document.createElement("label");
        label.setAttribute("for", id);
        label.textContent = field.label + (field.required ? " *" : "");
        wrap.appendChild(label);

        let input;

        if (field.type === "checkbox") {
            input = document.createElement("input");
            input.type = "checkbox";
            input.id = id;
            input.checked = !!value;
            input.addEventListener("change", () => {
                stationConf[field.key] = input.checked;
                onChange();
            });
        } else if (field.type === "select") {
            input = document.createElement("select");
            input.id = id;
            for (const opt of field.options) {
                const o = document.createElement("option");
                o.value = opt;
                o.textContent = opt === "" ? "(none)" : opt;
                if (value === opt || (value === undefined && opt === "")) o.selected = true;
                input.appendChild(o);
            }
            input.addEventListener("change", () => {
                if (input.value === "") {
                    delete stationConf[field.key];
                } else {
                    stationConf[field.key] = input.value;
                }
                onChange();
                // network_type changing affects which fields apply - full re-render.
                if (field.key === "network_type") {
                    renderForm(wrap.closest(".fs42-form-root"), stationConf, onChange);
                }
            });
        } else if (field.type === "lines") {
            input = document.createElement("textarea");
            input.id = id;
            input.rows = 4;
            input.value = Array.isArray(value) ? value.join("\n") : "";
            input.addEventListener("change", () => {
                stationConf[field.key] = linesToArray(input.value);
                onChange();
            });
        } else if (field.type === "json") {
            input = document.createElement("textarea");
            input.id = id;
            input.rows = 4;
            input.className = "fs42-json-field";
            input.value = value !== undefined ? JSON.stringify(value, null, 2) : "";
            const err = document.createElement("div");
            err.className = "fs42-field-error";
            input.addEventListener("change", () => {
                const text = input.value.trim();
                if (text === "") {
                    delete stationConf[field.key];
                    err.textContent = "";
                    onChange();
                    return;
                }
                try {
                    stationConf[field.key] = JSON.parse(text);
                    err.textContent = "";
                    onChange();
                } catch (e) {
                    err.textContent = `Invalid JSON: ${e.message}`;
                }
            });
            wrap.appendChild(input);
            if (field.help) {
                const help = document.createElement("div");
                help.className = "fs42-field-help";
                help.textContent = field.help;
                wrap.appendChild(help);
            }
            wrap.appendChild(err);
            return wrap;
        } else {
            input = document.createElement("input");
            input.type = field.type === "number" ? "number" : "text";
            input.id = id;
            input.value = value !== undefined && value !== null ? value : "";
            input.addEventListener("change", () => {
                if (input.value === "") {
                    delete stationConf[field.key];
                } else if (field.type === "number") {
                    const n = Number(input.value);
                    if (!Number.isNaN(n)) stationConf[field.key] = n;
                } else {
                    stationConf[field.key] = input.value;
                }
                onChange();
            });
        }

        wrap.appendChild(input);
        if (field.help) {
            const help = document.createElement("div");
            help.className = "fs42-field-help";
            help.textContent = field.help;
            wrap.appendChild(help);
        }
        return wrap;
    }

    function renderForm(container, stationConf, onChange) {
        container.innerHTML = "";
        container.classList.add("fs42-form-root");

        const networkType = stationConf.network_type || "standard";

        const tabBar = document.createElement("div");
        tabBar.className = "fs42-form-tabs";

        const panels = document.createElement("div");

        let activeTab = container.__activeTab || "basic";

        function showTab(tabId) {
            activeTab = tabId;
            container.__activeTab = tabId;
            [...tabBar.children].forEach(btn => btn.classList.toggle("active", btn.dataset.tab === tabId));
            [...panels.children].forEach(panel => {
                panel.style.display = (panel.dataset.tab === tabId) ? "block" : "none";
            });
        }

        for (const tab of TABS) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "pure-button";
            btn.dataset.tab = tab.id;
            btn.textContent = tab.label;
            btn.addEventListener("click", () => showTab(tab.id));
            tabBar.appendChild(btn);

            const panel = document.createElement("fieldset");
            panel.className = "pure-group fs42-form-panel";
            panel.dataset.tab = tab.id;

            const relevant = FIELDS.filter(f => f.tab === tab.id && fieldApplies(f, networkType));
            if (relevant.length === 0) {
                const none = document.createElement("p");
                none.className = "fs42-field-help";
                none.textContent = `No ${tab.label.toLowerCase()} fields apply to network type "${networkType}".`;
                panel.appendChild(none);
            }
            for (const field of relevant) {
                panel.appendChild(renderFieldInput(field, stationConf, onChange));
            }
            panels.appendChild(panel);
        }

        container.appendChild(tabBar);
        container.appendChild(panels);
        showTab(activeTab);
    }

    window.fs42Form = { render: renderForm };
})();

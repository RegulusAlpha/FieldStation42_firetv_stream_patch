// Visual day/hour schedule editor for "standard" network-type stations.
//
// Reuses existing scheduler primitives (tags / random_tags / sequence /
// marathon) rather than inventing new ones - see the plan doc's "Slot editor
// modal" section. The one new idea, nested tag paths (e.g.
// "G-Anime/Dragon Ball") as their own independently-schedulable tag, was
// already supported by the scheduler (tag_overrides documents this) - this
// UI is what makes that discoverable, via the content-folder browser.

(function () {
    const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
    const DAY_LABELS = { monday: "Monday", tuesday: "Tuesday", wednesday: "Wednesday", thursday: "Thursday", friday: "Friday", saturday: "Saturday", sunday: "Sunday" };
    const HOURS = Array.from({ length: 24 }, (_, i) => i);

    function fmtHour(h) {
        const ampm = h < 12 ? "AM" : "PM";
        let h12 = h % 12;
        if (h12 === 0) h12 = 12;
        return `${h12}:00 ${ampm}`;
    }

    // --- Day/template resolution -------------------------------------------

    function resolveDayHoursReadOnly(stationConf, day) {
        const val = stationConf[day];
        if (typeof val === "string") {
            return (stationConf.day_templates || {})[val] || {};
        }
        return val && typeof val === "object" ? val : {};
    }

    function dayTemplateName(stationConf, day) {
        return typeof stationConf[day] === "string" ? stationConf[day] : null;
    }

    // Materializes the day as its own inline hour-map if it's currently a
    // template reference, then returns that hour-map for mutation. Returns
    // the template name that was detached from, or null if it was already
    // inline.
    function detachDayForEdit(stationConf, day) {
        const val = stationConf[day];
        if (typeof val === "string") {
            const tmpl = (stationConf.day_templates || {})[val] || {};
            stationConf[day] = JSON.parse(JSON.stringify(tmpl));
            return val;
        }
        if (typeof val !== "object" || val === null) {
            stationConf[day] = {};
        }
        return null;
    }

    function describeSlot(slot) {
        if (!slot) return { text: "Off-air", badge: "" };
        if (slot.event === "signoff") return { text: "Sign-off", badge: "📴" };
        if (slot.tags === undefined) return { text: "(no tag)", badge: "" };

        const tagList = Array.isArray(slot.tags) ? slot.tags : [slot.tags];
        let badge = "";
        if (slot.marathon) badge = "🔁 marathon";
        else if (slot.sequence && slot.random_tags) badge = "🔀▶ sequential shuffle";
        else if (slot.sequence) badge = "▶ sequential";
        else if (slot.random_tags) badge = "🔀 shuffle";
        return { text: tagList.join(", "), badge };
    }

    // --- Content folder tree -------------------------------------------------

    // contentDir is passed explicitly (rather than relying on the server to
    // look the station up by name and use its persisted content_dir) so the
    // folder browser works both before a brand-new station has been saved
    // for the first time, and reflects an in-progress, unsaved edit to
    // content_dir made in the Form tab.
    function makeTreeFetcher(stationName, contentDir) {
        const cache = new Map();
        return async function fetchChildren(tagPath) {
            if (cache.has(tagPath)) return cache.get(tagPath);
            const params = new URLSearchParams();
            if (tagPath) params.set("path", tagPath);
            if (contentDir) params.set("content_dir", contentDir);
            const q = params.toString() ? `?${params.toString()}` : "";
            const data = await window.fs42Api.get(`content/${encodeURIComponent(stationName || "_new")}/tree${q}`);
            cache.set(tagPath, data.entries);
            return data.entries;
        };
    }

    function renderTreeNode(entry, fetchChildren, selected, onToggle, depth) {
        const li = document.createElement("li");
        li.style.marginLeft = `${depth * 1.25}em`;

        const row = document.createElement("div");
        row.className = "fs42-tree-row";

        let expandBtn = null;
        if (entry.has_children) {
            expandBtn = document.createElement("button");
            expandBtn.type = "button";
            expandBtn.className = "fs42-tree-expand";
            expandBtn.textContent = "▶";
            row.appendChild(expandBtn);
        } else {
            const spacer = document.createElement("span");
            spacer.className = "fs42-tree-spacer";
            row.appendChild(spacer);
        }

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.dataset.tagPath = entry.tag_path;
        checkbox.checked = selected.has(entry.tag_path);
        checkbox.addEventListener("change", () => onToggle(entry.tag_path, checkbox.checked));
        row.appendChild(checkbox);

        const label = document.createElement("span");
        label.textContent = entry.name;
        label.title = entry.tag_path;
        label.className = "fs42-tree-label";
        label.addEventListener("click", () => {
            checkbox.checked = !checkbox.checked;
            onToggle(entry.tag_path, checkbox.checked);
        });
        row.appendChild(label);

        li.appendChild(row);

        if (expandBtn) {
            let expanded = false;
            let childUl = null;
            expandBtn.addEventListener("click", async () => {
                if (!expanded) {
                    if (!childUl) {
                        childUl = document.createElement("ul");
                        childUl.className = "fs42-tree-list";
                        li.appendChild(childUl);
                        expandBtn.textContent = "…";
                        try {
                            const children = await fetchChildren(entry.tag_path);
                            childUl.innerHTML = "";
                            for (const child of children) {
                                childUl.appendChild(renderTreeNode(child, fetchChildren, selected, onToggle, depth + 1));
                            }
                        } catch (e) {
                            childUl.textContent = `Failed to load: ${e.message}`;
                        }
                    }
                    childUl.style.display = "";
                    expandBtn.textContent = "▼";
                    expanded = true;
                } else {
                    childUl.style.display = "none";
                    expandBtn.textContent = "▶";
                    expanded = false;
                }
            });
        }

        return li;
    }

    async function renderTreeRoot(container, fetchChildren, selected, onToggle) {
        container.innerHTML = "Loading...";
        try {
            const entries = await fetchChildren("");
            const ul = document.createElement("ul");
            ul.className = "fs42-tree-list fs42-tree-root";
            for (const entry of entries) {
                ul.appendChild(renderTreeNode(entry, fetchChildren, selected, onToggle, 0));
            }
            container.innerHTML = "";
            container.appendChild(ul);
        } catch (e) {
            container.textContent = `Could not load content folders: ${e.message}`;
        }
    }

    // --- Slot editor modal ---------------------------------------------------

    function determineInitialMode(slot) {
        if (!slot || slot.tags === undefined) {
            if (slot && slot.event === "signoff") return "offair";
            return slot ? "single" : "offair";
        }
        if (slot.marathon) return "marathon";
        if (slot.sequence && slot.random_tags) return "sequential_shuffle";
        if (slot.sequence) return "sequential";
        if (slot.random_tags) return "shuffle";
        return "single";
    }

    function openSlotEditor(opts) {
        const { stationName, contentDir, day, hour, slot, onSave } = opts;
        const fetchChildren = makeTreeFetcher(stationName, contentDir);

        const initialTags = slot && slot.tags !== undefined
            ? (Array.isArray(slot.tags) ? slot.tags.slice() : [slot.tags])
            : [];
        const selected = new Set(initialTags);
        let mode = determineInitialMode(slot);
        let sequenceName = (slot && slot.sequence) || "seq";
        let marathonCount = (slot && slot.marathon && slot.marathon.count) || 4;
        let marathonAlways = !slot || !slot.marathon || slot.marathon.chance === undefined || slot.marathon.chance >= 1;
        let marathonChance = (slot && slot.marathon && slot.marathon.chance) || 1;
        let marathonInOrder = !!(slot && slot.marathon && slot.sequence);
        let advancedJsonText = slot ? JSON.stringify(slot, null, 2) : "";
        let advancedOpen = false;

        const overlay = document.createElement("div");
        overlay.className = "fs42-modal-overlay";
        const modal = document.createElement("div");
        modal.className = "fs42-modal";
        overlay.appendChild(modal);

        function close() {
            document.body.removeChild(overlay);
        }

        // The folder tree is rendered once (see bottom of this function) and
        // never torn down on selection/mode changes - only checking/unchecking
        // a folder deep in the tree used to trigger a full modal re-render,
        // which collapsed every expanded node and re-fetched from scratch.
        // Everything below the tree (chips, mode, advanced JSON, actions) is
        // rebuilt on every change instead, via renderRest().

        const title = document.createElement("h3");
        title.textContent = `${DAY_LABELS[day]} — ${fmtHour(hour)}`;
        modal.appendChild(title);

        const sourceLabel = document.createElement("label");
        sourceLabel.textContent = "Content source (pick one or more folders/shows)";
        modal.appendChild(sourceLabel);

        const selectedBar = document.createElement("div");
        selectedBar.className = "fs42-selected-tags";
        modal.appendChild(selectedBar);

        const treeContainer = document.createElement("div");
        treeContainer.className = "fs42-tree-container";
        modal.appendChild(treeContainer);

        const restContainer = document.createElement("div");
        modal.appendChild(restContainer);

        function setTreeCheckbox(tagPath, checked) {
            const el = treeContainer.querySelector(`input[type="checkbox"][data-tag-path="${CSS.escape(tagPath)}"]`);
            if (el) el.checked = checked;
        }

        function renderChips() {
            selectedBar.innerHTML = "";
            if (selected.size === 0) {
                selectedBar.textContent = "Nothing selected yet.";
                return;
            }
            for (const tagPath of selected) {
                const chip = document.createElement("span");
                chip.className = "fs42-tag-chip";
                chip.textContent = tagPath;
                const x = document.createElement("button");
                x.type = "button";
                x.textContent = "×";
                x.addEventListener("click", () => {
                    selected.delete(tagPath);
                    setTreeCheckbox(tagPath, false);
                    onSelectionChanged();
                });
                chip.appendChild(x);
                selectedBar.appendChild(chip);
            }
        }

        function onSelectionChanged() {
            const singleOnlyModes = ["sequential", "marathon", "weekly"];
            const multiOnlyModes = ["shuffle", "sequential_shuffle"];
            if (selected.size !== 1 && singleOnlyModes.includes(mode)) {
                mode = selected.size > 1 ? "shuffle" : "single";
            } else if (selected.size < 2 && multiOnlyModes.includes(mode)) {
                mode = "single";
            }
            renderChips();
            renderRest();
        }

        function renderRest() {
            restContainer.innerHTML = "";
            const modal = restContainer; // keep the block below unchanged

            // --- Mode ---
            const modeLabel = document.createElement("label");
            modeLabel.textContent = "Mode";
            modal.appendChild(modeLabel);

            const singleShowOnly = selected.size === 1;
            const multiShowOnly = selected.size >= 2;
            const modeOptions = [
                { id: "single", text: "Single feed (least-played episode)", disabled: false },
                { id: "shuffle", text: "Shuffle among selected", disabled: !multiShowOnly },
                { id: "sequential_shuffle", text: "Sequential Shuffle (random show, each continues where it left off)", disabled: !multiShowOnly },
                { id: "sequential", text: "Sequential (play in order)", disabled: !singleShowOnly },
                { id: "marathon", text: "Marathon", disabled: !singleShowOnly },
                { id: "weekly", text: "Weekly episode (one airing per week)", disabled: !singleShowOnly },
                { id: "offair", text: "Off-air / clear this slot", disabled: false },
            ];

            const modeGroup = document.createElement("div");
            modeGroup.className = "fs42-mode-group";
            for (const opt of modeOptions) {
                const row = document.createElement("label");
                row.className = "fs42-mode-option";
                const radio = document.createElement("input");
                radio.type = "radio";
                radio.name = "fs42-slot-mode";
                radio.value = opt.id;
                radio.checked = mode === opt.id;
                radio.disabled = opt.disabled;
                radio.addEventListener("change", () => { mode = opt.id; renderRest(); });
                row.appendChild(radio);
                row.appendChild(document.createTextNode(" " + opt.text));
                modeGroup.appendChild(row);
            }
            modal.appendChild(modeGroup);

            if (mode === "sequential" || mode === "weekly" || mode === "sequential_shuffle") {
                const help = document.createElement("div");
                help.className = "fs42-field-help";
                if (mode === "weekly") {
                    help.textContent = "Plays the next episode of this show each time this exact slot airs. Make sure this show isn't also scheduled elsewhere if you want exactly one airing per week.";
                } else if (mode === "sequential_shuffle") {
                    help.textContent = "Randomly picks one of the selected shows each time, but each show remembers where it left off and plays its next episode - so shows are interleaved unpredictably without ever repeating or restarting a show.";
                } else {
                    help.textContent = "Plays episodes of this show in order, continuing from wherever it left off.";
                }
                modal.appendChild(help);

                const seqRow = document.createElement("div");
                seqRow.className = "fs42-advanced-field";
                const seqLbl = document.createElement("label");
                seqLbl.textContent = mode === "sequential_shuffle"
                    ? "Sequence name (advanced - leave as \"seq\" unless you need a second, independent set of positions across these same shows)"
                    : "Sequence name (advanced - leave as \"seq\" unless you need a second, independent sequence over the same show)";
                const seqInput = document.createElement("input");
                seqInput.type = "text";
                seqInput.value = sequenceName;
                seqInput.addEventListener("change", () => { sequenceName = seqInput.value.trim() || "seq"; });
                seqRow.appendChild(seqLbl);
                seqRow.appendChild(seqInput);
                modal.appendChild(seqRow);
            }

            if (mode === "marathon") {
                const countRow = document.createElement("div");
                const countLbl = document.createElement("label");
                countLbl.textContent = "Number of episodes";
                const countInput = document.createElement("input");
                countInput.type = "number";
                countInput.min = "1";
                countInput.value = marathonCount;
                countInput.addEventListener("change", () => { marathonCount = Math.max(1, parseInt(countInput.value, 10) || 1); });
                countRow.appendChild(countLbl);
                countRow.appendChild(countInput);
                modal.appendChild(countRow);

                const orderRow = document.createElement("label");
                orderRow.className = "fs42-mode-option";
                const orderCb = document.createElement("input");
                orderCb.type = "checkbox";
                orderCb.checked = marathonInOrder;
                orderCb.addEventListener("change", () => { marathonInOrder = orderCb.checked; });
                orderRow.appendChild(orderCb);
                orderRow.appendChild(document.createTextNode(" Play episodes in order (otherwise least-played first)"));
                modal.appendChild(orderRow);

                const alwaysRow = document.createElement("label");
                alwaysRow.className = "fs42-mode-option";
                const alwaysCb = document.createElement("input");
                alwaysCb.type = "checkbox";
                alwaysCb.checked = marathonAlways;
                alwaysCb.addEventListener("change", () => { marathonAlways = alwaysCb.checked; renderRest(); });
                alwaysRow.appendChild(alwaysCb);
                alwaysRow.appendChild(document.createTextNode(" Always trigger (uncheck for a random chance instead)"));
                modal.appendChild(alwaysRow);

                if (!marathonAlways) {
                    const chanceRow = document.createElement("div");
                    const chanceLbl = document.createElement("label");
                    chanceLbl.textContent = "Chance of marathon (0.0 - 1.0)";
                    const chanceInput = document.createElement("input");
                    chanceInput.type = "number";
                    chanceInput.min = "0";
                    chanceInput.max = "1";
                    chanceInput.step = "0.05";
                    chanceInput.value = marathonChance;
                    chanceInput.addEventListener("change", () => {
                        marathonChance = Math.min(1, Math.max(0, parseFloat(chanceInput.value) || 0));
                    });
                    chanceRow.appendChild(chanceLbl);
                    chanceRow.appendChild(chanceInput);
                    modal.appendChild(chanceRow);
                }
            }

            // --- Advanced (collapsed) ---
            const advToggle = document.createElement("button");
            advToggle.type = "button";
            advToggle.className = "pure-button";
            advToggle.textContent = advancedOpen ? "Hide advanced JSON" : "Advanced: edit this slot as JSON";
            advToggle.addEventListener("click", () => { advancedOpen = !advancedOpen; renderRest(); });
            modal.appendChild(advToggle);

            if (advancedOpen) {
                const help = document.createElement("div");
                help.className = "fs42-field-help";
                help.textContent = "If this is filled in, it's used as the entire slot definition instead of the options above (break_strategy, bumps, video_scramble_fx, overrides, etc. all go here).";
                modal.appendChild(help);
                const advTextarea = document.createElement("textarea");
                advTextarea.className = "fs42-json-field";
                advTextarea.rows = 8;
                advTextarea.value = advancedJsonText;
                advTextarea.addEventListener("input", () => { advancedJsonText = advTextarea.value; });
                modal.appendChild(advTextarea);
            }

            // --- Actions ---
            const actions = document.createElement("div");
            actions.className = "editor-actions";

            const saveBtn = document.createElement("button");
            saveBtn.type = "button";
            saveBtn.className = "pure-button pure-button-primary";
            saveBtn.textContent = "Apply";
            saveBtn.addEventListener("click", () => {
                if (advancedOpen && advancedJsonText.trim() !== "") {
                    let parsed;
                    try {
                        parsed = JSON.parse(advancedJsonText);
                    } catch (e) {
                        alert(`Invalid JSON: ${e.message}`);
                        return;
                    }
                    onSave(parsed);
                    close();
                    return;
                }

                let newSlot = null;
                if (mode !== "offair") {
                    if (selected.size === 0) {
                        alert("Pick at least one content folder, or choose Off-air.");
                        return;
                    }
                    const tagsValue = selected.size === 1 ? [...selected][0] : [...selected];
                    newSlot = { tags: tagsValue };
                    if (mode === "shuffle") {
                        newSlot.random_tags = true;
                    } else if (mode === "sequential_shuffle") {
                        newSlot.random_tags = true;
                        newSlot.sequence = sequenceName;
                    } else if (mode === "sequential" || mode === "weekly") {
                        newSlot.sequence = sequenceName;
                    } else if (mode === "marathon") {
                        newSlot.marathon = { count: marathonCount, chance: marathonAlways ? 1 : marathonChance };
                        if (marathonInOrder) newSlot.sequence = sequenceName;
                    }
                }
                onSave(newSlot);
                close();
            });
            actions.appendChild(saveBtn);

            const cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "pure-button";
            cancelBtn.textContent = "Cancel";
            cancelBtn.addEventListener("click", close);
            actions.appendChild(cancelBtn);

            modal.appendChild(actions);
        }

        // Render the tree exactly once; selection/mode changes only touch
        // renderChips()/renderRest() above, never this.
        if (!contentDir) {
            treeContainer.textContent = "Set a Content Directory in the Form tab first, then come back to pick folders here.";
        } else {
            renderTreeRoot(treeContainer, fetchChildren, selected, (tagPath, isChecked) => {
                if (isChecked) selected.add(tagPath); else selected.delete(tagPath);
                onSelectionChanged();
            });
        }

        renderChips();
        renderRest();
        document.body.appendChild(overlay);
    }

    // --- Day-template manager --------------------------------------------------

    function renderTemplateBar(container, stationConf, day, onChange) {
        container.innerHTML = "";
        container.className = "fs42-template-bar";

        const tmplName = dayTemplateName(stationConf, day);
        const templates = stationConf.day_templates || {};

        const status = document.createElement("span");
        if (tmplName) {
            const usedBy = DAYS.filter(d => stationConf[d] === tmplName);
            status.textContent = `Using template "${tmplName}" (shared with: ${usedBy.filter(d => d !== day).map(d => DAY_LABELS[d]).join(", ") || "no other days"})`;
        } else {
            status.textContent = "This day has its own inline schedule (not shared with any template).";
        }
        container.appendChild(status);

        if (tmplName) {
            const detachBtn = document.createElement("button");
            detachBtn.type = "button";
            detachBtn.className = "pure-button";
            detachBtn.textContent = "Detach into its own schedule";
            detachBtn.addEventListener("click", () => {
                detachDayForEdit(stationConf, day);
                onChange();
            });
            container.appendChild(detachBtn);
        } else {
            const saveAsBtn = document.createElement("button");
            saveAsBtn.type = "button";
            saveAsBtn.className = "pure-button";
            saveAsBtn.textContent = "Save as reusable template...";
            saveAsBtn.addEventListener("click", () => {
                const name = prompt("Template name:");
                if (!name) return;
                if (!stationConf.day_templates) stationConf.day_templates = {};
                stationConf.day_templates[name] = stationConf[day];
                stationConf[day] = name;
                onChange();
            });
            container.appendChild(saveAsBtn);

            if (Object.keys(templates).length > 0) {
                const applySelect = document.createElement("select");
                const noneOpt = document.createElement("option");
                noneOpt.value = "";
                noneOpt.textContent = "Apply existing template...";
                applySelect.appendChild(noneOpt);
                for (const name of Object.keys(templates)) {
                    const o = document.createElement("option");
                    o.value = name;
                    o.textContent = name;
                    applySelect.appendChild(o);
                }
                applySelect.addEventListener("change", () => {
                    if (applySelect.value) {
                        stationConf[day] = applySelect.value;
                        onChange();
                    }
                });
                container.appendChild(applySelect);
            }
        }

        const copyLabel = document.createElement("span");
        copyLabel.textContent = " Copy this day's schedule to:";
        container.appendChild(copyLabel);
        const copySelect = document.createElement("select");
        const copyNone = document.createElement("option");
        copyNone.value = "";
        copyNone.textContent = "(choose a day)";
        copySelect.appendChild(copyNone);
        for (const d of DAYS) {
            if (d === day) continue;
            const o = document.createElement("option");
            o.value = d;
            o.textContent = DAY_LABELS[d];
            copySelect.appendChild(o);
        }
        copySelect.addEventListener("change", () => {
            if (!copySelect.value) return;
            const hours = resolveDayHoursReadOnly(stationConf, day);
            stationConf[copySelect.value] = JSON.parse(JSON.stringify(hours));
            copySelect.value = "";
            onChange();
        });
        container.appendChild(copySelect);
    }

    // --- Build actions (reuse existing /build/* endpoints) ----------------------

    async function pollTask(basePath, taskId, logEl) {
        while (true) {
            const status = await window.fs42Api.get(`${basePath}/status/${taskId}`);
            logEl.textContent = status.log || "";
            if (status.status === "done" || status.status === "error" || status.error) {
                return status;
            }
            await new Promise(r => setTimeout(r, 1000));
        }
    }

    function renderBuildActions(container, stationName) {
        container.innerHTML = "";
        container.className = "fs42-build-actions";

        if (!stationName) {
            const note = document.createElement("div");
            note.className = "fs42-field-help";
            note.textContent = "Save this station first to enable building its schedule.";
            container.appendChild(note);
            return;
        }

        const logEl = document.createElement("pre");
        logEl.className = "fs42-build-log";

        const rebuildBtn = document.createElement("button");
        rebuildBtn.type = "button";
        rebuildBtn.className = "pure-button";
        rebuildBtn.textContent = "Rebuild Schedule";
        rebuildBtn.addEventListener("click", async () => {
            rebuildBtn.disabled = true;
            try {
                const { task_id } = await window.fs42Api.post(`build/schedule/reset/${encodeURIComponent(stationName)}`, {});
                await pollTask("build/schedule/reset", task_id, logEl);
            } catch (e) {
                logEl.textContent = `Error: ${e.message}`;
            }
            rebuildBtn.disabled = false;
        });

        const addTimeBtn = document.createElement("button");
        addTimeBtn.type = "button";
        addTimeBtn.className = "pure-button";
        addTimeBtn.textContent = "Add 1 Week";
        addTimeBtn.addEventListener("click", async () => {
            addTimeBtn.disabled = true;
            try {
                const { task_id } = await window.fs42Api.post(`build/schedule/add_time/1w/${encodeURIComponent(stationName)}`, {});
                await pollTask("build/schedule/add_time", task_id, logEl);
            } catch (e) {
                logEl.textContent = `Error: ${e.message}`;
            }
            addTimeBtn.disabled = false;
        });

        container.appendChild(rebuildBtn);
        container.appendChild(addTimeBtn);
        container.appendChild(logEl);
    }

    // --- Top-level render --------------------------------------------------

    function render(container, currentConfig, stationName, markDirty) {
        const stationConf = currentConfig.station_conf;

        if ((stationConf.network_type || "standard") !== "standard") {
            container.innerHTML = "";
            const note = document.createElement("p");
            note.textContent = `The visual schedule editor is only available for "standard" network types. This station is "${stationConf.network_type}" - use the JSON view for its scheduling-related settings.`;
            container.appendChild(note);
            return;
        }

        container.innerHTML = "";
        const activeDay = container.__activeDay || "monday";

        const dayTabs = document.createElement("div");
        dayTabs.className = "fs42-day-tabs";
        for (const d of DAYS) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "pure-button" + (d === activeDay ? " active" : "");
            btn.textContent = DAY_LABELS[d];
            btn.addEventListener("click", () => { container.__activeDay = d; render(container, currentConfig, stationName, markDirty); });
            dayTabs.appendChild(btn);
        }
        container.appendChild(dayTabs);

        const templateBar = document.createElement("div");
        container.appendChild(templateBar);
        renderTemplateBar(templateBar, stationConf, activeDay, () => render(container, currentConfig, stationName, markDirty));

        const hours = resolveDayHoursReadOnly(stationConf, activeDay);

        const grid = document.createElement("div");
        grid.className = "fs42-hour-grid";
        for (const hour of HOURS) {
            const slot = hours[String(hour)];
            const { text, badge } = describeSlot(slot);

            const row = document.createElement("div");
            row.className = "fs42-hour-row";

            const hourLabel = document.createElement("span");
            hourLabel.className = "fs42-hour-label";
            hourLabel.textContent = fmtHour(hour);
            row.appendChild(hourLabel);

            const summary = document.createElement("span");
            summary.className = "fs42-hour-summary";
            summary.textContent = badge ? `${badge} — ${text}` : text;
            row.appendChild(summary);

            const editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "pure-button";
            editBtn.textContent = "Edit";
            editBtn.addEventListener("click", () => {
                openSlotEditor({
                    stationName,
                    contentDir: stationConf.content_dir,
                    day: activeDay,
                    hour,
                    slot,
                    onSave: (newSlot) => {
                        detachDayForEdit(stationConf, activeDay);
                        const dayHours = stationConf[activeDay];
                        if (newSlot === null) {
                            delete dayHours[String(hour)];
                        } else {
                            dayHours[String(hour)] = newSlot;
                        }
                        markDirty();
                        render(container, currentConfig, stationName, markDirty);
                    },
                });
            });
            row.appendChild(editBtn);

            grid.appendChild(row);
        }
        container.appendChild(grid);

        const buildSection = document.createElement("div");
        container.appendChild(buildSection);
        renderBuildActions(buildSection, stationName);
    }

    window.fs42Schedule = { render };
})();

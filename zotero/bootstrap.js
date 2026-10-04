let observerID = null;
let shuttingDown = false;
const PDF_MARKER = "ZoteroLocalFileConnector-v0.7";
const SNAPSHOT_MARKER = "ZoteroLocalFileConnectorSnapshot-v0.8.5";

const pending = new Set();
const registeredEndpoints = new Set();

function log(message, error) {
    try {
        Zotero.debug(`[Local File Connector v0.11.14] ${message}`);
        if (error) Zotero.logError(error);
    }
    catch (_) {}
}

function getExtra(item) {
    try {
        return String(item.getField("extra") || "");
    }
    catch (_) {
        return "";
    }
}

function parseSnapshotMarker(extra) {
    if (!extra.includes(SNAPSHOT_MARKER)) return null;

    let path = null;
    let url = null;

    for (let line of extra.split(/\r?\n/)) {
        if (line.startsWith("Snapshot-Path: ")) {
            path = line.slice("Snapshot-Path: ".length).trim();
        }
        else if (line.startsWith("Snapshot-URL: ")) {
            url = line.slice("Snapshot-URL: ".length).trim();
        }
    }

    if (!path) return null;
    return { path, url };
}

function stripSnapshotMarker(extra) {
    return extra
        .split(/\r?\n/)
        .filter(line =>
            line !== SNAPSHOT_MARKER &&
            !line.startsWith("Snapshot-Path: ") &&
            !line.startsWith("Snapshot-URL: ")
        )
        .join("\n")
        .trim();
}

async function removeTempFile(path) {
    try {
        if (typeof IOUtils !== "undefined" && IOUtils.remove) {
            await IOUtils.remove(path, { ignoreAbsent: true });
            return;
        }
    }
    catch (e) {
        log(`Could not remove temporary snapshot file ${path}`, e);
    }
}

async function importSnapshot(parentID) {
    if (pending.has(`snapshot:${parentID}`)) return;
    pending.add(`snapshot:${parentID}`);

    try {
        // Let saveItems + updateSession finish so the parent is already in
        // the collection chosen in Firefox.
        await Zotero.Promise.delay(1400);
        if (shuttingDown) return;

        let parent = await Zotero.Items.getAsync(parentID);
        if (!parent || !parent.isRegularItem()) return;

        let extra = getExtra(parent);
        let marker = parseSnapshotMarker(extra);
        if (!marker) return;

        log(`Importing webpage snapshot for item ${parentID} from ${marker.path}`);

        // Use Zotero's dedicated snapshot importer rather than generic
        // importFromFile(). This is important because Zotero defines a snapshot
        // as LINK_MODE_IMPORTED_URL + text/html, which controls the proper
        // Snapshot icon and isSnapshotAttachment() behaviour.
        let attachment = await Zotero.Attachments.importSnapshotFromFile({
            file: marker.path,
            url: marker.url || parent.getField("url") || "",
            title: "Snapshot",
            contentType: "text/html",
            charset: "utf-8",
            parentItemID: parent.id,
            singleFile: true
        });

        if (!attachment) {
            throw new Error("Zotero.Attachments.importSnapshotFromFile returned no attachment");
        }

        // Remove our implementation metadata from Extra once the import worked.
        parent.setField("extra", stripSnapshotMarker(extra));
        await parent.saveTx();

        await removeTempFile(marker.path);

        log(`Snapshot imported as attachment ${attachment.id} for item ${parentID}`);
    }
    catch (e) {
        log(`Snapshot import failed for item ${parentID}`, e);
    }
    finally {
        pending.delete(`snapshot:${parentID}`);
    }
}


function parsePDFProvenance(extra) {
    let out = { url: "", accessedAt: "" };
    for (let line of String(extra || "").split(/\r?\n/)) {
        if (line.startsWith("LFB-Provenance-URL: ")) out.url = line.slice("LFB-Provenance-URL: ".length).trim();
        else if (line.startsWith("LFB-Provenance-Accessed: ")) out.accessedAt = line.slice("LFB-Provenance-Accessed: ".length).trim();
    }
    return out;
}

function bibliographicURL(value) {
    value = String(value || "").trim();
    if (/^file:/i.test(value) || /^[a-z]:[\\/]/i.test(value) || /^\\\\/.test(value)) return "";
    return value;
}

async function applyPDFProvenance(item, provenance, label) {
    if (!item) return { url: "", accessDate: "" };
    let url = bibliographicURL((provenance && provenance.url) || "");
    let accessDate = normalizeAccessDateForZotero((provenance && provenance.accessedAt) || "");
    if (url) item.setField("url", url);
    if (accessDate) item.setField("accessDate", accessDate);
    await item.saveTx();
    try { await item.reload(["itemData"], true); } catch (_) {}
    let savedURL = "";
    let savedAccessDate = "";
    try { savedURL = item.getField("url") || ""; } catch (_) {}
    try { savedAccessDate = item.getField("accessDate") || ""; } catch (_) {}
    log(`${label || "PDF"} provenance after save: URL=${savedURL || "<empty>"}; Accessed=${savedAccessDate || "<empty>"}`);
    if (url && !savedURL) throw new Error(`${label || "PDF"}: Zotero did not retain the URL`);
    if (accessDate && !savedAccessDate) throw new Error(`${label || "PDF"}: Zotero did not retain Accessed ${accessDate}`);
    return { url: savedURL, accessDate: savedAccessDate };
}

async function recognizePDF(itemID) {
    if (pending.has(`pdf:${itemID}`)) return;
    pending.add(`pdf:${itemID}`);

    try {
        await Zotero.Promise.delay(900);
        if (shuttingDown) return;

        let attachment = await Zotero.Items.getAsync(itemID);
        if (!attachment || !attachment.isAttachment()) return;
        if (attachment.attachmentContentType !== "application/pdf") return;

        let parentID = attachment.parentItemID || attachment.parentID;
        if (!parentID) return;

        let parent = await Zotero.Items.getAsync(parentID);
        if (!parent || !parent.isRegularItem()) return;

        let extra = getExtra(parent);
        if (!extra.includes(PDF_MARKER)) return;
        let provenance = parsePDFProvenance(extra);

        let collectionIDs = [];
        try { collectionIDs = parent.getCollections ? parent.getCollections() : []; }
        catch (e) { log("Could not read PDF destination collections", e); }

        // Detach the PDF from our temporary marker parent first. This reproduces
        // Zotero Connector's standalone-PDF state — the state in which Zotero's
        // own UI correctly shows attachment URL + Accessed.
        attachment.parentID = false;
        if (attachment.setCollections) attachment.setCollections(collectionIDs);
        await attachment.saveTx();
        attachment = await Zotero.Items.getAsync(itemID);

        // Apply the archived provenance immediately to the standalone attachment.
        // This is deliberately done in the same Connector-created attachment path
        // that already works for Send direct to Zotero.
        await applyPDFProvenance(attachment, provenance, "Standalone PDF before recognition");

        try { parent.deleted = true; await parent.saveTx(); }
        catch (e) { log("Could not trash temporary PDF parent", e); }

        await Zotero.Promise.delay(500);
        let recognizedParent = null;
        if (Zotero.RecognizeDocument && typeof Zotero.RecognizeDocument.recognizeItems === "function") {
            try {
                await Zotero.RecognizeDocument.recognizeItems([attachment]);
                // Recognition can finish asynchronously. Poll for a resulting parent,
                // but do not require one: grey literature may legitimately remain standalone.
                let deadline = Date.now() + 30000;
                while (Date.now() < deadline) {
                    attachment = await Zotero.Items.getAsync(itemID);
                    let rid = attachment && (attachment.parentItemID || attachment.parentID);
                    if (rid) {
                        try { recognizedParent = await Zotero.Items.getAsync(rid); } catch (_) {}
                        if (recognizedParent) break;
                    }
                    await Zotero.Promise.delay(500);
                }
            }
            catch (e) { log(`PDF recognition failed for ${itemID}`, e); }
        }

        // Recognition may reload/alter the attachment, so apply provenance AGAIN
        // to the final attachment. If a real bibliographic parent was created,
        // apply it there as well.
        attachment = await Zotero.Items.getAsync(itemID);
        await applyPDFProvenance(attachment, provenance, "Final PDF attachment");
        if (recognizedParent) {
            await applyPDFProvenance(recognizedParent, provenance, "Recognized PDF parent");
        }
        log(`PDF recognition/provenance finished for ${itemID}; parent=${recognizedParent ? recognizedParent.id : "none"}`);
    }
    catch (e) {
        log(`PDF recognition/provenance failed for ${itemID}`, e);
    }
    finally {
        pending.delete(`pdf:${itemID}`);
    }
}

async function handleItem(id, event) {
    let item;
    try {
        item = await Zotero.Items.getAsync(id);
    }
    catch (_) {
        return;
    }

    if (!item) return;

    // Web Page snapshot parent
    if (item.isRegularItem && item.isRegularItem()) {
        let extra = getExtra(item);
        if (extra.includes(SNAPSHOT_MARKER)) {
            importSnapshot(item.id).catch(e => log("Unhandled snapshot error", e));
        }
        return;
    }

    // PDF child created by our existing import workflow
    if (item.isAttachment &&
        item.isAttachment() &&
        item.attachmentContentType === "application/pdf") {
        recognizePDF(item.id).catch(e => log("Unhandled PDF recognition error", e));
    }
}

const observer = {
    notify(event, type, ids, extraData) {
        if (shuttingDown || type !== "item") return;
        if (event !== "add" && event !== "modify") return;

        for (let id of ids) {
            handleItem(id, event).catch(
                e => log(`Notifier processing error for item ${id}`, e)
            );
        }
    }
};



function parseJSONRequest(requestData) {
    if (typeof requestData === "object" && requestData !== null) return requestData;
    if (typeof requestData === "string" && requestData.trim()) {
        let value = requestData;
        try { if (value.startsWith("%")) value = decodeURIComponent(value); } catch (_) {}
        return JSON.parse(value);
    }
    return {};
}

function registerEndpoint(path, handler) {
    Zotero.Server.Endpoints[path] = function() {};
    Zotero.Server.Endpoints[path].prototype = handler;
    registeredEndpoints.add(path);
    log(`Registered endpoint ${path}`);
}

function unregisterEndpoints() {
    for (let path of registeredEndpoints) {
        try { delete Zotero.Server.Endpoints[path]; } catch (_) {}
    }
    registeredEndpoints.clear();
}

function targetParts(target) {
    let text = String(target || "");
    if (text.startsWith("C")) return {kind: "collection", id: parseInt(text.slice(1), 10)};
    if (text.startsWith("L")) return {kind: "library", id: parseInt(text.slice(1), 10)};
    return null;
}

async function applyDestinationTarget(item, target) {
    let parsed = targetParts(target);
    if (!item || !parsed) return;
    if (parsed.kind === "collection" && Number.isFinite(parsed.id)) {
        try { item.setCollections([parsed.id]); await item.saveTx(); } catch (e) { log("Could not apply destination collection", e); }
    }
}

async function findOrCreateCollection(name, parentTarget) {
    name = String(name || "").trim();
    if (!name) throw new Error("Collection name is empty");

    let libraryID = Zotero.Libraries.userLibraryID;
    let parentID = false;
    let parsed = targetParts(parentTarget);

    if (parsed && parsed.kind === "library" && Number.isFinite(parsed.id)) {
        libraryID = parsed.id;
    }
    else if (parsed && parsed.kind === "collection" && Number.isFinite(parsed.id)) {
        let parent = await Zotero.Collections.getAsync(parsed.id);
        if (!parent) throw new Error("Parent Zotero collection no longer exists");
        libraryID = parent.libraryID;
        parentID = parent.id;
    }

    let collections = [];
    try {
        collections = Zotero.Collections.getByLibrary(libraryID) || [];
    }
    catch (_) {
        try { collections = Zotero.Collections.getAll(libraryID) || []; } catch (_) { collections = []; }
    }

    for (let c of collections) {
        try {
            if (c.name === name && (c.parentID || false) === (parentID || false)) {
                return {id: c.id, key: c.key, libraryID: c.libraryID, created: false};
            }
        }
        catch (_) {}
    }

    let collection = new Zotero.Collection();
    collection.libraryID = libraryID;
    collection.name = name;
    if (parentID) collection.parentID = parentID;
    await collection.saveTx();
    return {id: collection.id, key: collection.key, libraryID: collection.libraryID, created: true};
}


async function getCollectionInventory(libraryID) {
    let ids = [];
    try {
        ids = await Zotero.DB.columnQueryAsync(
            "SELECT collectionID FROM collections WHERE libraryID=? ORDER BY collectionName COLLATE NOCASE",
            [libraryID]
        );
    }
    catch (e) {
        log("Could not query collection IDs", e);
        return [];
    }

    let out = [];
    for (let id of ids) {
        try {
            let c = await Zotero.Collections.getAsync(id);
            if (!c) continue;
            out.push({
                id: c.id,
                key: c.key,
                name: c.name || "Untitled collection",
                parentID: c.parentID || null,
                libraryID: c.libraryID
            });
        }
        catch (e) {
            log(`Could not read collection ${id}`, e);
        }
    }
    return out;
}

async function attachmentFilePath(item) {
    try {
        if (item && typeof item.getFilePathAsync === "function") {
            return await item.getFilePathAsync();
        }
        if (item && typeof item.getFilePath === "function") {
            return item.getFilePath();
        }
    }
    catch (e) {
        log(`Could not resolve attachment path for ${item && item.id}`, e);
    }
    return null;
}

async function getAttachmentInventory(libraryID) {
    let ids = [];
    try {
        ids = await Zotero.DB.columnQueryAsync(
            "SELECT ia.itemID FROM itemAttachments ia JOIN items i ON i.itemID=ia.itemID WHERE i.libraryID=? ORDER BY ia.itemID",
            [libraryID]
        );
    }
    catch (e) {
        log("Could not query Zotero attachment IDs", e);
        throw e;
    }

    let out = [];
    for (let id of ids) {
        try {
            let attachment = await Zotero.Items.getAsync(id);
            if (!attachment || !attachment.isAttachment || !attachment.isAttachment()) continue;

            let parentID = attachment.parentItemID || attachment.parentID || null;
            let owner = attachment;
            let parentTitle = "";
            let sourceURL = "";
            let accessDate = "";
            if (parentID) {
                try {
                    let parent = await Zotero.Items.getAsync(parentID);
                    if (parent) {
                        owner = parent;
                        try { parentTitle = parent.getField("title") || ""; } catch (_) {}
                        try { sourceURL = parent.getField("url") || ""; } catch (_) {}
                        try { accessDate = parent.getField("accessDate") || ""; } catch (_) {}
                    }
                }
                catch (_) {}
            }
            // Parent bibliographic items normally own URL/Accessed, but standalone
            // attachments can own both fields themselves. Preserve attachment-level
            // provenance whenever the parent does not provide it.
            if (!sourceURL) {
                try { sourceURL = attachment.getField("url") || attachment.attachmentURL || ""; } catch (_) {}
            }
            if (!accessDate) {
                try { accessDate = attachment.getField("accessDate") || ""; } catch (_) {}
            }

            let collectionIDs = [];
            try {
                collectionIDs = owner.getCollections ? owner.getCollections() : [];
            }
            catch (_) { collectionIDs = []; }

            let path = await attachmentFilePath(attachment);
            let title = "";
            try { title = attachment.getField("title") || ""; } catch (_) {}
            if (!title && path) {
                try { title = PathUtils.filename(path); } catch (_) {}
            }

            out.push({
                id: attachment.id,
                key: attachment.key,
                libraryID: attachment.libraryID,
                parentID,
                parentTitle: parentTitle || "",
                title: title || "Attachment",
                path: path || null,
                contentType: attachment.attachmentContentType || null,
                linkMode: attachment.attachmentLinkMode,
                sourceURL: sourceURL || "",
                accessDate: accessDate || "",
                collectionIDs: Array.from(collectionIDs || [])
            });
        }
        catch (e) {
            log(`Could not inventory attachment ${id}`, e);
        }
    }
    return out;
}

async function exportAttachmentInventory() {
    let libraryID = Zotero.Libraries.userLibraryID;
    let collections = await getCollectionInventory(libraryID);
    let attachments = await getAttachmentInventory(libraryID);
    return {
        libraryID,
        libraryName: "My Library",
        collections,
        attachments
    };
}


function creatorFromArchiveName(name) {
    name = String(name || "").replace(/^\s*(?:by\s+)?/i, "").trim();
    if (!name) return null;
    try {
        if (Zotero.Utilities && typeof Zotero.Utilities.cleanAuthor === "function") {
            let c = Zotero.Utilities.cleanAuthor(name, "author", true);
            if (c) return c;
        }
    }
    catch (_) {}
    if (name.includes(",")) {
        let parts = name.split(",");
        return { lastName: parts.shift().trim(), firstName: parts.join(",").trim(), creatorType: "author" };
    }
    let parts = name.split(/\s+/).filter(Boolean);
    if (parts.length > 1) {
        let lastName = parts.pop();
        return { firstName: parts.join(" "), lastName, creatorType: "author" };
    }
    return { name, creatorType: "author" };
}

async function importArchivedWebPage(path, sourceURL, accessedAt, archiveTitle, archiveAuthors) {
    sourceURL = bibliographicURL(sourceURL);
    accessedAt = String(accessedAt || "").trim();
    archiveTitle = String(archiveTitle || "").trim();
    archiveAuthors = Array.isArray(archiveAuthors) ? archiveAuthors : [];

    let parent = new Zotero.Item("webpage");
    parent.libraryID = Zotero.Libraries.userLibraryID;
    parent.setField("title", archiveTitle || PathUtils.filename(path).replace(/\.html?$/i, ""));
    if (sourceURL) parent.setField("url", sourceURL);
    if (accessedAt) {
        // Zotero accepts ISO-style access dates; keep the capture timestamp rather than
        // replacing it with the later Send-to-Zotero time.
        try { parent.setField("accessDate", accessedAt); } catch (_) {}
    }
    let creators = archiveAuthors.map(creatorFromArchiveName).filter(Boolean);
    if (creators.length) {
        try { parent.setCreators(creators); } catch (e) { log("Could not set archive authors", e); }
    }
    await parent.saveTx();

    let attachment = await Zotero.Attachments.importSnapshotFromFile({
        file: path,
        url: sourceURL || "",
        title: "Snapshot",
        contentType: "text/html",
        charset: "utf-8",
        parentItemID: parent.id,
        singleFile: true
    });
    if (!attachment) {
        try { parent.deleted = true; await parent.saveTx(); } catch (_) {}
        throw new Error("Zotero could not attach the local HTML archive");
    }
    return { attachment, parent, usedArchiveMetadata: !!(archiveTitle || creators.length), recognized: true };
}


function normalizeAccessDateForZotero(value) {
    value = String(value || "").trim();
    if (!value) return "";
    try {
        if (value === "CURRENT_TIMESTAMP") {
            return Zotero.Date && Zotero.Date.dateToSQL ? Zotero.Date.dateToSQL(new Date(), true) : value;
        }
        let d = new Date(value);
        if (!Number.isNaN(d.getTime()) && Zotero.Date && Zotero.Date.dateToSQL) {
            return Zotero.Date.dateToSQL(d, true);
        }
    } catch (_) {}
    // Zotero's accessDate field is stored in SQL-style form. If the incoming
    // value is already close to that form, normalize the separator/timezone.
    let m = value.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
    if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
    return value;
}

async function persistAndVerifyAttachmentProvenance(attachment, sourceURL, accessedAt) {
    let normalizedAccessDate = normalizeAccessDateForZotero(accessedAt);
    if (sourceURL) attachment.setField("url", sourceURL);
    if (normalizedAccessDate) attachment.setField("accessDate", normalizedAccessDate);
    await attachment.saveTx();

    // Re-fetch the item after the transaction so we verify what Zotero actually
    // persisted rather than trusting the in-memory object.
    let fresh = await Zotero.Items.getAsync(attachment.id);
    if (!fresh) throw new Error("Zotero could not reload the imported PDF attachment");
    let savedURL = "";
    let savedAccessDate = "";
    try { savedURL = fresh.getField("url") || ""; } catch (_) {}
    try { savedAccessDate = fresh.getField("accessDate") || ""; } catch (_) {}

    if (sourceURL && !savedURL) {
        throw new Error("Zotero did not persist the PDF attachment URL");
    }
    if (normalizedAccessDate && !savedAccessDate) {
        throw new Error(`Zotero did not persist the PDF attachment Accessed date (${normalizedAccessDate})`);
    }
    return { attachment: fresh, savedURL, savedAccessDate, normalizedAccessDate };
}

async function waitForPDFParent(attachmentID, timeoutMS = 30000) {
    let deadline = Date.now() + timeoutMS;
    while (Date.now() < deadline) {
        let attachment = await Zotero.Items.getAsync(attachmentID);
        if (!attachment) break;
        let parentID = attachment.parentItemID || attachment.parentID || null;
        if (parentID) {
            let parent = null;
            try { parent = await Zotero.Items.getAsync(parentID); } catch (_) {}
            if (parent && parent.isRegularItem && parent.isRegularItem()) {
                return { attachment, parent, parentID };
            }
        }
        await Zotero.Promise.delay(500);
    }
    let attachment = await Zotero.Items.getAsync(attachmentID);
    return { attachment, parent: null, parentID: null };
}

async function createFallbackPDFParent(attachment, title, sourceURL, accessedAt) {
    let parent = new Zotero.Item("document");
    parent.libraryID = attachment.libraryID || Zotero.Libraries.userLibraryID;
    parent.setField("title", String(title || "").trim() || PathUtils.filename(await attachment.getFilePathAsync()).replace(/\.pdf$/i, ""));
    try { if (sourceURL) parent.setField("url", sourceURL); } catch (_) {}
    try { if (accessedAt) parent.setField("accessDate", accessedAt); } catch (_) {}
    await parent.saveTx();
    try {
        if (attachment.setCollections) attachment.setCollections([]);
        attachment.parentID = parent.id;
        if ("parentItemID" in attachment) {
            try { attachment.parentItemID = parent.id; } catch (_) {}
        }
        await attachment.saveTx();
    }
    catch (e) {
        try { parent.deleted = true; await parent.saveTx(); } catch (_) {}
        throw e;
    }
    return parent;
}

async function importLocalFileToZotero(path, sourceURL, accessedAt, archiveTitle, archiveAuthors, displayTitle) {
    path = String(path || "");
    sourceURL = bibliographicURL(sourceURL);
    accessedAt = String(accessedAt || "").trim();
    displayTitle = String(displayTitle || "").trim();
    if (!path) throw new Error("No local file path was supplied");
    let lower = path.toLowerCase();
    let isHTML = /\.html?$/.test(lower);

    if (isHTML) {
        let web = await importArchivedWebPage(path, sourceURL, accessedAt, archiveTitle, archiveAuthors);
        let attachmentTitle = "";
        let parentTitle = "";
        try { attachmentTitle = web.attachment.getField("title") || ""; } catch (_) {}
        try { parentTitle = web.parent.getField("title") || ""; } catch (_) {}
        return {
            attachment: web.attachment, recognized: true, usedArchiveMetadata: web.usedArchiveMetadata,
            attachmentTitle, parentID: web.parent.id, parentKey: web.parent.key, parentTitle
        };
    }

    let libraryID = Zotero.Libraries.userLibraryID;
    let attachment = null;
    let opts = { file: path, libraryID };
    try { attachment = await Zotero.Attachments.importFromFile(opts); }
    catch (e) {
        log("importFromFile with libraryID failed, retrying basic import", e);
        attachment = await Zotero.Attachments.importFromFile({ file: path });
    }
    if (!attachment) throw new Error("Zotero did not create an attachment");

    let contentType = attachment.attachmentContentType || "";
    let isPDF = contentType === "application/pdf" || /\.pdf$/i.test(path);
    let recognized = false;
    let parent = null;
    let parentID = null;

    // Preserve Local File Bookmarks provenance on the attachment itself *before*
    // recognition. Zotero supports URL/Accessed on standalone PDF attachments,
    // so an unrecognized PDF must not lose its archive metadata.
    let attachmentSavedURL = "";
    let attachmentSavedAccessDate = "";
    if (isPDF) {
        try {
            let persisted = await persistAndVerifyAttachmentProvenance(attachment, sourceURL, accessedAt);
            attachment = persisted.attachment || attachment;
            attachmentSavedURL = persisted.savedURL || "";
            attachmentSavedAccessDate = persisted.savedAccessDate || "";
        }
        catch (e) {
            log("Could not persist PDF provenance metadata on attachment", e);
            // Do not silently continue and claim success. Recognition can still run,
            // but the caller must be told that the archival metadata was not saved.
            throw e;
        }
    }

    if (isPDF && Zotero.RecognizeDocument &&
        typeof Zotero.RecognizeDocument.recognizeItems === "function") {
        try {
            // Recognition may return before Zotero has attached the PDF to its new
            // bibliographic parent. Wait for that relationship rather than checking once.
            await Zotero.RecognizeDocument.recognizeItems([attachment]);
            let waited = await waitForPDFParent(attachment.id, 30000);
            attachment = waited.attachment || attachment;
            parent = waited.parent;
            parentID = waited.parentID;
            recognized = !!parent;
        }
        catch (e) { log("PDF metadata recognition failed after Send to Zotero", e); }
    }

    if (!parentID) {
        parentID = attachment.parentItemID || attachment.parentID || null;
        if (parentID) { try { parent = await Zotero.Items.getAsync(parentID); } catch (_) {} }
    }

    if (parent) {
        try { if (sourceURL) parent.setField("url", sourceURL); } catch (_) {}
        try {
            let normalizedAccessDate = normalizeAccessDateForZotero(accessedAt);
            if (normalizedAccessDate) parent.setField("accessDate", normalizedAccessDate);
        } catch (_) {}
        try { await parent.saveTx(); } catch (e) { log("Could not save PDF provenance metadata on parent", e); }
    }

    // Recognize Document can update/reload the attachment and may wipe attachment
    // URL/accessDate values. Re-apply provenance to the FINAL attachment after
    // recognition has completed (or failed), then verify what Zotero now stores.
    if (isPDF) {
        let finalAttachment = await Zotero.Items.getAsync(attachment.id);
        if (!finalAttachment) throw new Error("Zotero could not reload the final PDF attachment");
        let persistedFinal = await persistAndVerifyAttachmentProvenance(finalAttachment, sourceURL, accessedAt);
        attachment = persistedFinal.attachment || finalAttachment;
        attachmentSavedURL = persistedFinal.savedURL || attachmentSavedURL;
        attachmentSavedAccessDate = persistedFinal.savedAccessDate || attachmentSavedAccessDate;
    }

    let attachmentTitle = "";
    let parentTitle = "";
    try { attachmentTitle = attachment.getField("title") || ""; } catch (_) {}
    try { parentTitle = parent ? (parent.getField("title") || "") : ""; } catch (_) {}
    return {
        attachment, recognized: !!recognized, usedArchiveMetadata: false,
        attachmentTitle, parentID, parentKey: parent ? parent.key : null, parentTitle,
        attachmentSavedURL, attachmentSavedAccessDate
    };
}

async function importCurrentPage(url, title, authors, accessedAt) {
    url = String(url || "").trim();
    title = String(title || "").trim();
    authors = Array.isArray(authors) ? authors : [];
    accessedAt = String(accessedAt || "").trim();
    if (!url) throw new Error("No current-page URL was supplied");

    let parent = new Zotero.Item("webpage");
    parent.libraryID = Zotero.Libraries.userLibraryID;
    parent.setField("title", title || url);
    parent.setField("url", url);
    if (accessedAt) {
        try { parent.setField("accessDate", accessedAt); } catch (_) {}
    }
    let creators = authors.map(creatorFromArchiveName).filter(Boolean);
    if (creators.length) {
        try { parent.setCreators(creators); } catch (e) { log("Could not set current-page authors", e); }
    }
    await parent.saveTx();
    return parent;
}

async function targetLibraryID(target) {
    let parsed = targetParts(target);
    if (!parsed) return Zotero.Libraries.userLibraryID;
    if (parsed.kind === "library" && Number.isFinite(parsed.id)) return parsed.id;
    if (parsed.kind === "collection" && Number.isFinite(parsed.id)) {
        let collection = await Zotero.Collections.getAsync(parsed.id);
        if (collection) return collection.libraryID;
    }
    return Zotero.Libraries.userLibraryID;
}

async function detectRemoteContentType(url) {
    let contentType = "";
    try {
        let response = await Zotero.HTTP.request("HEAD", url, {
            followRedirects: true,
            timeout: 15000
        });
        try { contentType = response.getResponseHeader("Content-Type") || ""; } catch (_) {}
    }
    catch (e) {
        log("Remote HEAD request failed; using URL fallback", e);
    }
    return String(contentType || "").split(";")[0].trim().toLowerCase();
}

async function importRemoteURLToZotero(data) {
    let url = String(data.url || "").trim();
    let title = String(data.title || "").trim();
    let authors = Array.isArray(data.authors) ? data.authors : [];
    let accessedAt = String(data.accessedAt || "").trim();
    let target = String(data.target || "");
    if (!/^https?:\/\//i.test(url)) throw new Error("Remote import requires an HTTP or HTTPS URL");

    let libraryID = await targetLibraryID(target);
    let contentType = await detectRemoteContentType(url);
    let isPDF = contentType === "application/pdf" || /\.pdf(?:$|[?#])/i.test(url);

    if (isPDF) {
        let attachment = await Zotero.Attachments.importFromURL({
            libraryID,
            url,
            title: title || "PDF",
            contentType: "application/pdf"
        });
        if (!attachment) throw new Error("Zotero did not create the PDF attachment");

        // Keep source provenance even if Recognize Document later reparents it.
        try {
            let persisted = await persistAndVerifyAttachmentProvenance(attachment, url, accessedAt);
            attachment = persisted.attachment || attachment;
        } catch (e) { log("Could not persist remote PDF provenance", e); }

        let parent = null;
        if (Zotero.RecognizeDocument &&
            typeof Zotero.RecognizeDocument.recognizeItems === "function") {
            try {
                await Zotero.RecognizeDocument.recognizeItems([attachment]);
                let waited = await waitForPDFParent(attachment.id, 30000);
                attachment = waited.attachment || attachment;
                parent = waited.parent || null;
            }
            catch (e) { log("Remote PDF recognition failed", e); }
        }

        if (parent) {
            try { parent.setField("url", url); } catch (_) {}
            try {
                let d = normalizeAccessDateForZotero(accessedAt);
                if (d) parent.setField("accessDate", d);
            } catch (_) {}
            try { await parent.saveTx(); } catch (_) {}
            await applyDestinationTarget(parent, target);
        }
        else {
            await applyDestinationTarget(attachment, target);
        }

        try {
            let finalAttachment = await Zotero.Items.getAsync(attachment.id);
            if (finalAttachment) {
                await persistAndVerifyAttachmentProvenance(finalAttachment, url, accessedAt);
            }
        } catch (e) { log("Could not reapply remote PDF provenance", e); }

        return {
            kind: "pdf",
            itemID: attachment.id,
            parentID: parent ? parent.id : null,
            title: parent ? (parent.getField("title") || title) : (title || "PDF"),
            contentType: "application/pdf"
        };
    }

    // Webpage: Zotero creates the parent and downloads its own snapshot directly.
    let parent = new Zotero.Item("webpage");
    parent.libraryID = libraryID;
    parent.setField("title", title || url);
    parent.setField("url", url);
    if (accessedAt) {
        try { parent.setField("accessDate", accessedAt); } catch (_) {}
    }
    let creators = authors.map(creatorFromArchiveName).filter(Boolean);
    if (creators.length) {
        try { parent.setCreators(creators); } catch (_) {}
    }
    await parent.saveTx();

    try {
        let snapshot = await Zotero.Attachments.importFromURL({
            libraryID,
            parentItemID: parent.id,
            url,
            title: "Snapshot",
            contentType: "text/html"
        });
        if (!snapshot) throw new Error("Zotero did not create a webpage snapshot");
        await applyDestinationTarget(parent, target);
        return {
            kind: "webpage",
            parentID: parent.id,
            parentKey: parent.key,
            title: parent.getField("title") || title || url,
            snapshotID: snapshot.id,
            contentType: "text/html"
        };
    }
    catch (e) {
        // Avoid leaving an empty parent when direct capture fails; Firefox will
        // immediately retry using the browser-side fallback.
        try { await Zotero.Items.trashTx(parent.id); } catch (_) {}
        throw e;
    }
}


async function requestStashLibrarySelfUpdate() {
    const ADDON_ID = "local-file-connector-helper-v088@chatgpt.local";
    let AddonManager;
    try {
        ({ AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs"));
    }
    catch (_) {
        try { ({ AddonManager } = ChromeUtils.import("resource://gre/modules/AddonManager.jsm")); }
        catch (e) { throw new Error("Zotero's add-on updater is unavailable: " + (e && e.message ? e.message : e)); }
    }
    const addon = await AddonManager.getAddonByID(ADDON_ID);
    if (!addon) throw new Error("StashLibrary Zotero integration is not installed.");
    return await new Promise((resolve, reject) => {
        let settled = false;
        const finish = (value, error) => {
            if (settled) return;
            settled = true;
            if (error) reject(error); else resolve(value);
        };
        const listener = {
            onUpdateAvailable(_addon, install) {
                try {
                    if (!install) return finish({ok:false,error:"Zotero found an update but did not provide an installer."});
                    install.addListener({
                        onInstallEnded(_install, installedAddon) { finish({ok:true,status:"installed",version:String(installedAddon && installedAddon.version || "")}); },
                        onInstallFailed(_install) { finish(null,new Error("Zotero could not install the StashLibrary update.")); },
                        onDownloadFailed(_install) { finish(null,new Error("Zotero could not download the StashLibrary update.")); }
                    });
                    install.install();
                }
                catch (e) { finish(null,e); }
            },
            onNoUpdateAvailable() { finish({ok:true,status:"no_update",version:String(addon.version||"")}); },
            onUpdateFinished(_addon, error) {
                if (error) finish(null,new Error(String(error)));
            }
        };
        try { addon.findUpdates(listener, AddonManager.UPDATE_WHEN_USER_REQUESTED); }
        catch (e) { finish(null,e); }
        setTimeout(() => finish(null,new Error("Zotero's StashLibrary update check timed out.")), 30000);
    });
}

function registerMigrationEndpoints() {
    registerEndpoint("/local-file-connector/version", {
        supportedMethods: ["POST", "GET"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            sendResponseCallback(200, "application/json", JSON.stringify({ok:true, version:"0.1.0", protocolVersion:1}));
        }
    });
    registerEndpoint("/local-file-connector/update", {
        supportedMethods: ["POST"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            try {
                let result = await requestStashLibrarySelfUpdate();
                sendResponseCallback(200, "application/json", JSON.stringify({ok:true, ...result}));
            }
            catch (e) {
                log("StashLibrary self-update failed", e);
                sendResponseCallback(500, "application/json", JSON.stringify({ok:false,error:e && e.message ? e.message : String(e)}));
            }
        }
    });
    registerEndpoint("/local-file-connector/export-attachments", {
        supportedMethods: ["POST"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            try {
                let inventory = await exportAttachmentInventory();
                sendResponseCallback(200, "application/json", JSON.stringify({
                    ok: true,
                    ...inventory
                }));
            }
            catch (e) {
                log("export-attachments failed", e);
                sendResponseCallback(500, "application/json", JSON.stringify({
                    ok: false,
                    error: e && e.message ? e.message : String(e)
                }));
            }
        }
    });

    registerEndpoint("/local-file-connector/import-file", {
        supportedMethods: ["POST"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            try {
                let data = parseJSONRequest(requestData);
                let result = await importLocalFileToZotero(
                    data.path, data.sourceURL || data.sourceUrl || "", data.accessedAt || "",
                    data.archiveTitle || "", data.archiveAuthors || [], data.displayTitle || ""
                );
                let destinationItem = null;
                if (result.parentID) { try { destinationItem = await Zotero.Items.getAsync(result.parentID); } catch (_) {} }
                if (!destinationItem) destinationItem = result.attachment;
                await applyDestinationTarget(destinationItem, data.target || "");
                let item = result.attachment;
                sendResponseCallback(200, "application/json", JSON.stringify({
                    ok: true,
                    itemID: item.id,
                    itemKey: item.key,
                    attachmentTitle: result.attachmentTitle || "",
                    parentID: result.parentID || null,
                    parentKey: result.parentKey || null,
                    parentTitle: result.parentTitle || "",
                    recognized: !!result.recognized,
                    usedArchiveMetadata: !!result.usedArchiveMetadata,
                    attachmentSavedURL: result.attachmentSavedURL || "",
                    attachmentSavedAccessDate: result.attachmentSavedAccessDate || ""
                }));
            }
            catch (e) {
                log("import-file failed", e);
                sendResponseCallback(500, "application/json", JSON.stringify({ok:false,error:e && e.message ? e.message : String(e)}));
            }
        }
    });


    registerEndpoint("/local-file-connector/import-remote-url", {
        supportedMethods: ["POST"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            try {
                let data = parseJSONRequest(requestData);
                let result = await importRemoteURLToZotero(data);
                sendResponseCallback(200, "application/json", JSON.stringify({
                    ok: true,
                    ...result
                }));
            }
            catch (e) {
                log("import-remote-url failed", e);
                sendResponseCallback(500, "application/json", JSON.stringify({
                    ok: false,
                    error: e && e.message ? e.message : String(e)
                }));
            }
        }
    });

    registerEndpoint("/local-file-connector/import-current-page", {
        supportedMethods: ["POST"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            try {
                let data = parseJSONRequest(requestData);
                let parent = await importCurrentPage(data.url || "", data.title || "", data.authors || [], data.accessedAt || "");
                let parentTitle = "";
                try { parentTitle = parent.getField("title") || ""; } catch (_) {}
                sendResponseCallback(200, "application/json", JSON.stringify({
                    ok: true,
                    parentID: parent.id,
                    parentKey: parent.key,
                    parentTitle
                }));
            }
            catch (e) {
                log("import-current-page failed", e);
                sendResponseCallback(500, "application/json", JSON.stringify({ok:false,error:e && e.message ? e.message : String(e)}));
            }
        }
    });

    registerEndpoint("/local-file-connector/ensure-collection", {
        supportedMethods: ["POST"],
        supportedDataTypes: ["application/json", "text/plain"],
        init: async function(requestData, sendResponseCallback) {
            try {
                let data = parseJSONRequest(requestData);
                let collection = await findOrCreateCollection(data.name, data.parentTarget);
                sendResponseCallback(200, "application/json", JSON.stringify({
                    ok: true,
                    target: "C" + collection.id,
                    collectionID: collection.id,
                    collectionKey: collection.key,
                    libraryID: collection.libraryID,
                    created: collection.created
                }));
            }
            catch (e) {
                log("ensure-collection failed", e);
                sendResponseCallback(500, "application/json", JSON.stringify({
                    ok: false,
                    error: e && e.message ? e.message : String(e)
                }));
            }
        }
    });
}

async function startup({ id, version, rootURI }, reason) {
    shuttingDown = false;
    await Zotero.initializationPromise;

    observerID = Zotero.Notifier.registerObserver(
        observer,
        ["item"],
        "local-file-connector-v085",
        1
    );

    registerMigrationEndpoints();

    log(`Started v${version} on Zotero ${Zotero.version}`);
}

function shutdown({ id, version, rootURI }, reason) {
    shuttingDown = true;

    if (observerID) {
        try {
            Zotero.Notifier.unregisterObserver(observerID);
        }
        catch (e) {
            log("Could not unregister observer", e);
        }
        observerID = null;
    }

    unregisterEndpoints();
    pending.clear();
    log("Stopped");
}

function install() {}
function uninstall() {}

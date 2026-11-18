import apiClient from './abscribeAPI'

// Fields that link a saved prompt node to its Firestore logging records
// (questionGenerations / difficultyEstimates). The two commit calls below
// post ``messageEntry`` itself as the request body, so these are forwarded
// exactly when the caller put them on the entry (HintViewer merges them in
// from ``linkMeta`` + ``userName``). Nothing else is added to the payload.
const PROMPT_LINK_FIELDS = ['userPromptText', 'userPromptHash', 'generationRunId', 'userName']

function pickLinkFields(source) {
    const out = {}
    if (!source) return out
    for (const name of PROMPT_LINK_FIELDS) {
        if (source[name] !== undefined) out[name] = source[name]
    }
    return out
}

export const promptHintLibraryService = {
    async getTextbookLibraryPromptList(spreadsheetId) {
        let response = await apiClient.get(`/api/prompt-library/active?spreadsheetId=${encodeURIComponent(spreadsheetId)}`)
        return response.data
    },

    async getAllActivePrompts(spreadsheetId) {
        let response = await apiClient.get(`/api/prompt-library/fetch-all-active-vfr567uhfr?spreadsheetId=${encodeURIComponent(spreadsheetId)}`)
        return response.data
    },

    async getLessonLibraryPromptList(spreadsheetId, lessonName) {
        let response = await apiClient.get(`/api/prompt-library/active?spreadsheetId=${encodeURIComponent(spreadsheetId)}&lessonName=${encodeURIComponent(lessonName)}`)
        console.log(response.data)
        return response.data
    },

    async commitPromptToSharedLibrary(userId, messageEntry, lessonName, sheetLink, lessonsTested, temperature, model) {
        messageEntry.userId = userId
        messageEntry.lessonName = lessonName
        messageEntry.isTextbookLevel = messageEntry.lessonName == null
        messageEntry.spreadsheetId = sheetLink
        messageEntry.lessonsTested = lessonsTested
        messageEntry.temperature = temperature 
        messageEntry.model = model
        // Link fields (userPromptText, userPromptHash, generationRunId, userName)
        // ride along when present; the entry is the body, so keep them explicit.
        const body = { ...messageEntry, ...pickLinkFields(messageEntry) }
        return await apiClient.post(`/api/prompt-library/commit-prompt`, body)
    },

    async commitPromptFromScratchpad(userId, messageEntry, lessonName, sheetLink, lessonsTested) {
        messageEntry.userId = userId
        messageEntry.lessonName = lessonName
        messageEntry.isTextbookLevel = messageEntry.lessonName == null
        messageEntry.spreadsheetId = sheetLink
        messageEntry.lessonsTested = lessonsTested
        const body = { ...messageEntry, ...pickLinkFields(messageEntry) }
        return await apiClient.post(`/api/prompt-library/commit-scratchpad-prompt`, body)
    },

    async toggleLikePrompt(id, userId) {
        const response = await apiClient.post(`/api/prompt-library/toggle-like-prompt`, {userId: userId, promptId: id})
        return response
    },

    async deletePrompt(id) {
        const response = await apiClient.post(`/api/prompt-library/archive-admin-vfr567uhfr`, {promptId: id})
        return response
    },

    async deleteAllPrompts(spreadsheetId) {
        const response = await apiClient.post(`/api/prompt-library/archive-all-admin-vfr567uhfr`, {spreadsheetId: spreadsheetId})
        return response
    },

    async updateLessonsTested(promtId, lessonsTested) {
        const response = await apiClient.put(`/api/prompt-library/update-lessons-tested`, {promptId: promtId, newLessons: lessonsTested});
        return response;
    },

    async archivePrompt(promptId, userId) {
        const response = await apiClient.put(`/api/prompt-library/archive-prompt`, {userId: userId, promptId: promptId})
        return response
    },

    async visualisePromptLibraryTree(sheetId) {
        const response = await apiClient.get(`/api/prompt-library/visualize-scratchpad-tree?spreadsheetId=${sheetId}`);
        return response;
    }
}
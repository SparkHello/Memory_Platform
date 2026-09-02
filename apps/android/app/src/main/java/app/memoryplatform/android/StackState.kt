package app.memoryplatform.android

import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import org.json.JSONObject

/** Last status() JSON from the Python runtime, shared by the service and the UI. */
object StackState {
    const val MEMORY_PORT = 2026
    const val MODEL_PORT = 2030
    const val CONSOLE_URL = "http://127.0.0.1:$MEMORY_PORT/"

    // Read synchronously by the service (LiveData.postValue lands later on the
    // main thread, which let two back-to-back start intents both see "stopped").
    @Volatile private var current = JSONObject("""{"state":"stopped"}""")
    private val _status = MutableLiveData(current)
    val status: LiveData<JSONObject> = _status

    fun update(json: String) {
        val value = JSONObject(json)
        current = value
        _status.postValue(value)
    }

    fun state(): String = current.optString("state", "stopped")
}

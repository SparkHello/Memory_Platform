package app.memoryplatform.android

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the gateways after a reboot if the user left them running. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val prefs = context.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE)
        if (prefs.getBoolean(MainActivity.PREF_AUTOSTART, false)) GatewayService.start(context)
    }
}

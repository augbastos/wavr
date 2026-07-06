package dev.wavr.mobile;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import dev.wavr.mobile.wavrnet.WavrNetPlugin;
import dev.wavr.mobile.securestorage.WavrSecureStoragePlugin;
import dev.wavr.mobile.wavrsensor.WavrSensorPlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(WavrNetPlugin.class);
        registerPlugin(WavrSecureStoragePlugin.class);
        registerPlugin(WavrSensorPlugin.class);
        super.onCreate(savedInstanceState);
    }
}

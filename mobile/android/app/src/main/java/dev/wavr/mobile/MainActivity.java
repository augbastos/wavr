package dev.wavr.mobile;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import dev.wavr.mobile.wavrnet.WavrNetPlugin;
import dev.wavr.mobile.securestorage.WavrSecureStoragePlugin;
import dev.wavr.mobile.wavrsensor.WavrSensorPlugin;
import dev.wavr.mobile.wavrbluetooth.WavrBluetoothPlugin;
import dev.wavr.mobile.wavrupdate.WavrUpdatePlugin;
import dev.wavr.mobile.wavrhealth.WavrHealthPlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(WavrNetPlugin.class);
        registerPlugin(WavrSecureStoragePlugin.class);
        registerPlugin(WavrSensorPlugin.class);
        registerPlugin(WavrBluetoothPlugin.class);
        registerPlugin(WavrUpdatePlugin.class);
        registerPlugin(WavrHealthPlugin.class);
        super.onCreate(savedInstanceState);
    }
}

package dev.wavr.mobile;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import dev.wavr.mobile.wavrnet.WavrNetPlugin;
import dev.wavr.mobile.securestorage.WavrSecureStoragePlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(WavrNetPlugin.class);
        registerPlugin(WavrSecureStoragePlugin.class);
        super.onCreate(savedInstanceState);
    }
}

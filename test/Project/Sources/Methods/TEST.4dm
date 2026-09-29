//%attributes = {}
C_COLLECTION:C1488($devices)
C_OBJECT:C1216($device)

$devices:=hid_enumerate 

For each ($device;$devices)
	  //warning: if you open your keyboard or track pad, it will become temporarily unresponsive!
	$status:=hid_open_path ($device.path)
	If ($status.success)
		
		hid_set_nonblocking ($status.device;1)
		hid_close ($status.device)
	End if 
End for each 

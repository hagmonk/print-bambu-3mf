package bambu

import "encoding/xml"

// 3MF metadata XML is decoded into fixed structs. Go's encoding/xml matches on
// element local-name (ignoring namespaces unless specified), which suits both
// the namespace-less Metadata/*.config files and the namespaced 3dmodel.model.

// xMeta is a <metadata key=".." value=".."/> element. Some metadata carry no
// key (e.g. <metadata face_count=".."/>); those have an empty Key.
type xMeta struct {
	Key   string `xml:"key,attr"`
	Value string `xml:"value,attr"`
}

// metaValue returns the value of the first direct-child metadata with the given
// key, mirroring ElementTree's `.//metadata[@key='..']` (first in document
// order). The object's own name/extruder metadata precede any nested <part>
// metadata, so direct children are sufficient and equivalent.
func metaValue(metas []xMeta, key string) (string, bool) {
	for _, m := range metas {
		if m.Key == key {
			return m.Value, true
		}
	}
	return "", false
}

// modelSettings mirrors Metadata/model_settings.config.
type modelSettings struct {
	XMLName xml.Name  `xml:"config"`
	Objects []xObject `xml:"object"`
	Plates  []xPlate  `xml:"plate"`
}

type xObject struct {
	ID       string  `xml:"id,attr"`
	Metadata []xMeta `xml:"metadata"`
}

type xPlate struct {
	Metadata  []xMeta         `xml:"metadata"`
	Instances []xModelInstAux `xml:"model_instance"`
}

type xModelInstAux struct {
	Metadata []xMeta `xml:"metadata"`
}

func parseModelSettings(data []byte) (*modelSettings, error) {
	var ms modelSettings
	if err := xml.Unmarshal(data, &ms); err != nil {
		return nil, err
	}
	return &ms, nil
}

// sliceInfo mirrors Metadata/slice_info.config (used only for printer_model_id).
type sliceInfo struct {
	XMLName xml.Name        `xml:"config"`
	Plates  []sliceInfoPlate `xml:"plate"`
}

type sliceInfoPlate struct {
	Metadata []xMeta `xml:"metadata"`
}

func parseSliceInfo(data []byte) (*sliceInfo, error) {
	var si sliceInfo
	if err := xml.Unmarshal(data, &si); err != nil {
		return nil, err
	}
	return &si, nil
}

// model3D mirrors the parts of 3D/3dmodel.model used by plate discovery: the
// ordered build items, each referencing an object id.
type model3D struct {
	XMLName xml.Name `xml:"model"`
	Build   struct {
		Items []struct {
			ObjectID string `xml:"objectid,attr"`
		} `xml:"item"`
	} `xml:"build"`
}

func parseModel3D(data []byte) (*model3D, error) {
	var m model3D
	if err := xml.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

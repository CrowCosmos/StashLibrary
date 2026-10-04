package main

import (
    "encoding/binary"
    "fmt"
    "os"
)

func align(v, a uint32) uint32 {
    if a == 0 { return v }
    return (v + a - 1) &^ (a - 1)
}

func u16(b []byte, o int) uint16 { return binary.LittleEndian.Uint16(b[o:o+2]) }
func u32(b []byte, o int) uint32 { return binary.LittleEndian.Uint32(b[o:o+4]) }
func p16(b []byte, o int, v uint16) { binary.LittleEndian.PutUint16(b[o:o+2], v) }
func p32(b []byte, o int, v uint32) { binary.LittleEndian.PutUint32(b[o:o+4], v) }

func main() {
    if len(os.Args) != 3 {
        fmt.Fprintln(os.Stderr, "usage: manifestpatch <exe> <manifest.xml>")
        os.Exit(2)
    }
    exePath, manifestPath := os.Args[1], os.Args[2]
    data, err := os.ReadFile(exePath)
    if err != nil { panic(err) }
    manifest, err := os.ReadFile(manifestPath)
    if err != nil { panic(err) }
    if len(data) < 0x100 || string(data[:2]) != "MZ" { panic("not a PE executable") }
    pe := int(u32(data, 0x3c))
    if pe+24 > len(data) || string(data[pe:pe+4]) != "PE\x00\x00" { panic("invalid PE header") }
    coff := pe+4
    nsec := int(u16(data, coff+2))
    optSize := int(u16(data, coff+16))
    opt := coff+20
    if opt+optSize > len(data) { panic("truncated optional header") }
    magic := u16(data,opt)
    var dataDir int
    switch magic {
    case 0x20b: dataDir=opt+112
    case 0x10b: dataDir=opt+96
    default: panic("unsupported PE optional header")
    }
    sectionAlignment:=u32(data,opt+32)
    fileAlignment:=u32(data,opt+36)
    sizeOfImageOff:=opt+56
    sizeOfInitDataOff:=opt+8
    secTable:=opt+optSize
    newSecHeader:=secTable+nsec*40
    if nsec < 1 || newSecHeader+40 > len(data) { panic("invalid section table") }

    firstRaw:=uint32(len(data))
    var lastVA, lastSpan, lastRawEnd uint32
    for i:=0;i<nsec;i++ {
        sh:=secTable+i*40
        vsize:=u32(data,sh+8)
        va:=u32(data,sh+12)
        rawSize:=u32(data,sh+16)
        rawPtr:=u32(data,sh+20)
        if rawPtr != 0 && rawPtr < firstRaw { firstRaw=rawPtr }
        if va >= lastVA { lastVA=va; lastSpan=vsize; if rawSize>lastSpan { lastSpan=rawSize } }
        if rawPtr+rawSize > lastRawEnd { lastRawEnd=rawPtr+rawSize }
    }
    if uint32(newSecHeader+40) > firstRaw { panic("not enough PE header room for .rsrc section") }

    // Resource tree: RT_MANIFEST(24) -> ID 1 -> language 1033 -> data entry.
    const rootOff=uint32(0)
    const typeOff=uint32(24)
    const nameOff=uint32(48)
    const dataEntryOff=uint32(72)
    const payloadOff=uint32(88)
    vsize:=payloadOff+uint32(len(manifest))
    rawSize:=align(vsize,fileAlignment)
    va:=align(lastVA+align(lastSpan,sectionAlignment),sectionAlignment)
    rawPtr:=align(lastRawEnd,fileAlignment)

    rsrc:=make([]byte,rawSize)
    // root directory and entry
    p16(rsrc,int(rootOff+14),1)
    p32(rsrc,int(rootOff+16),24)
    p32(rsrc,int(rootOff+20),0x80000000|typeOff)
    // type directory and ID=1 entry
    p16(rsrc,int(typeOff+14),1)
    p32(rsrc,int(typeOff+16),1)
    p32(rsrc,int(typeOff+20),0x80000000|nameOff)
    // name directory and language entry
    p16(rsrc,int(nameOff+14),1)
    p32(rsrc,int(nameOff+16),1033)
    p32(rsrc,int(nameOff+20),dataEntryOff)
    // IMAGE_RESOURCE_DATA_ENTRY
    p32(rsrc,int(dataEntryOff),va+payloadOff)
    p32(rsrc,int(dataEntryOff+4),uint32(len(manifest)))
    p32(rsrc,int(dataEntryOff+8),65001)
    copy(rsrc[payloadOff:],manifest)

    need:=int(rawPtr+rawSize)
    if len(data)<need { data=append(data,make([]byte,need-len(data))...) }
    copy(data[rawPtr:rawPtr+rawSize],rsrc)

    sh:=newSecHeader
    for i:=0;i<40;i++ { data[sh+i]=0 }
    copy(data[sh:sh+8],[]byte(".rsrc\x00\x00\x00"))
    p32(data,sh+8,vsize)
    p32(data,sh+12,va)
    p32(data,sh+16,rawSize)
    p32(data,sh+20,rawPtr)
    p32(data,sh+36,0x40000040) // initialized data | read
    p16(data,coff+2,uint16(nsec+1))
    p32(data,sizeOfImageOff,align(va+vsize,sectionAlignment))
    p32(data,sizeOfInitDataOff,u32(data,sizeOfInitDataOff)+rawSize)
    // IMAGE_DIRECTORY_ENTRY_RESOURCE = 2
    p32(data,dataDir+2*8,va)
    p32(data,dataDir+2*8+4,vsize)

    if err:=os.WriteFile(exePath,data,0755);err!=nil { panic(err) }
}

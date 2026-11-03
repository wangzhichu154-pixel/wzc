#include "trading/ml_io.hpp"
#include <array>
#include <cerrno>
#include <charconv>
#include <fcntl.h>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

namespace trading {
MarketExporter::MarketExporter(const std::filesystem::path& target):target_(target) {
    if(target_.empty()||std::filesystem::exists(target_)||std::filesystem::is_symlink(target_))
        throw ProtocolError("market export destination must not already exist");
    auto pattern=target_.string()+".partial.XXXXXX";
    std::vector<char> buffer(pattern.begin(),pattern.end());buffer.push_back('\0');
    descriptor_=::mkstemp(buffer.data());
    if(descriptor_<0)throw ProtocolError("cannot create private market export temporary file");
    temporary_=buffer.data();
    try{write_line(std::string(market_export_header)+"\n");}
    catch(...){::close(descriptor_);descriptor_=-1;::unlink(temporary_.c_str());throw;}
}
MarketExporter::~MarketExporter(){if(descriptor_>=0)::close(descriptor_);if(!committed_&&!temporary_.empty())::unlink(temporary_.c_str());}
void MarketExporter::write_line(const std::string& line) {
    std::size_t offset=0;
    while(offset<line.size()) {
        const auto count=::write(descriptor_,line.data()+offset,line.size()-offset);
        if(count<0&&errno==EINTR)continue;
        if(count<=0)throw ProtocolError("market export write failed");
        offset+=static_cast<std::size_t>(count);
    }
}
void MarketExporter::observe(const Event& e,const Book& book,std::uint16_t locate,std::uint64_t ordinal,std::uint64_t logical) {
    const auto top=book.top(locate);
    const bool valid=book.initialized()&&top.valid;
    const bool tradable=book.tradable(locate);
    const bool changed=top.bid!=previous_.bid||top.ask!=previous_.ask||top.bid_qty!=previous_.bid_qty||top.ask_qty!=previous_.ask_qty||
        valid!=previous_valid_||tradable!=previous_tradable_;
    const bool status=e.type=='S'||e.type=='H'||e.type=='h';
    if(!observed_||changed||status) {
        std::ostringstream row;
        row<<ordinal<<','<<e.timestamp_ns<<','<<logical<<','<<locate<<','<<top.bid<<','<<top.ask<<','<<top.bid_qty<<','<<top.ask_qty
           <<','<<(valid?1:0)<<','<<(tradable?1:0)<<'\n';
        write_line(row.str());++rows_;
    }
    previous_=top;previous_valid_=valid;previous_tradable_=tradable;observed_=true;
}
void MarketExporter::commit() {
    if(committed_)throw ProtocolError("market export already committed");
    if(::fsync(descriptor_)!=0)throw ProtocolError("market export fsync failed");
    const auto fd=descriptor_;descriptor_=-1;
    if(::close(fd)!=0)throw ProtocolError("market export close failed");
    // Both names are in the same directory. link() refuses an existing target,
    // including a symlink, and publishes the complete file atomically on Linux.
    if(::link(temporary_.c_str(),target_.c_str())!=0)throw ProtocolError("market export publish failed; destination must be absent");
    ::unlink(temporary_.c_str());committed_=true;
}
bool SignalReader::read_line(std::string& line) {
    line.clear();char ch{};
    while(input_.get(ch)) {
        if(ch=='\n'){if(!line.empty()&&line.back()=='\r')line.pop_back();return true;}
        if(line.size()>=256)throw ProtocolError("signal CSV line exceeds 256 bytes");
        line.push_back(ch);
    }
    if(input_.bad())throw ProtocolError("signal CSV read failure");
    if(!line.empty()&&line.back()=='\r')line.pop_back();
    return !line.empty();
}
static std::uint64_t decimal(const std::string& text) {
    std::uint64_t result{};
    const auto parsed=std::from_chars(text.data(),text.data()+text.size(),result);
    if(text.empty()||parsed.ec!=std::errc{}||parsed.ptr!=text.data()+text.size())throw ProtocolError("signal CSV requires unsigned decimal integers");
    return result;
}
void SignalReader::advance() {
    std::string line;
    if(!read_line(line)){next_.reset();return;}
    std::array<std::string,4> fields;
    std::size_t start=0;
    for(std::size_t i=0;i<fields.size();++i) {
        const auto comma=line.find(',',start);
        if((i+1<fields.size()&&comma==std::string::npos)||(i+1==fields.size()&&comma!=std::string::npos))
            throw ProtocolError("signal CSV must have exactly four columns");
        fields[i]=line.substr(start,comma==std::string::npos?comma:comma-start);
        start=comma==std::string::npos?line.size():comma+1;
    }
    SideSignal signal;signal.ordinal=decimal(fields[0]);signal.expires_ns=decimal(fields[3]);
    if(signal.ordinal==0||signal.ordinal<=previous_ordinal_)throw ProtocolError("signal ordinals must be positive, unique and increasing");
    if((fields[1]!="0"&&fields[1]!="1")||(fields[2]!="0"&&fields[2]!="1"))throw ProtocolError("signal side flags must be exactly 0 or 1");
    signal.allow_bid=fields[1]=="1";signal.allow_ask=fields[2]=="1";
    previous_ordinal_=signal.ordinal;next_=signal;
}
SignalReader::SignalReader(const std::filesystem::path& path) {
    std::error_code error;
    if(!std::filesystem::is_regular_file(path,error)||error)throw ProtocolError("signal input must be a regular file");
    const auto bytes=std::filesystem::file_size(path,error);
    if(error||bytes>128*1024*1024ULL)throw ProtocolError("signal input exceeds 128 MiB");
    input_.open(path,std::ios::binary);if(!input_)throw ProtocolError("cannot open signal CSV");
    std::string header;if(!read_line(header)||header!=signal_file_header)throw ProtocolError("unsupported signal CSV header/schema");
    advance();
}
std::optional<SideSignal> SignalReader::at(std::uint64_t ordinal) {
    if(!next_||next_->ordinal>ordinal)return std::nullopt;
    if(next_->ordinal<ordinal)throw ProtocolError("signal ordinal was not consumed at its exact event");
    const auto result=next_;++consumed_;advance();return result;
}
void SignalReader::finish() const {if(next_)throw ProtocolError("signal ordinal lies beyond the capture");}
}
